"""
School-admin endpoints — manage teachers, classrooms and school settings.
Accessible only by school_admin role users (scoped to their own school).
"""
from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from backend.api.deps import CurrentUser, SessionDep, assert_tenant, require_roles
from backend.core.security import hash_password
from backend.db.models import (
    Classroom, ParentStudent, PeriodTime, SUBJECTS, Student,
    TeacherSubjectAssignment, Tenant, TimetableEntry, User, UserRole,
)

router = APIRouter(prefix="/school", tags=["school-admin"])
SchoolAdminDep = Annotated[User, Depends(require_roles(UserRole.school_admin))]


# ── Schemas ──────────────────────────────────────────────


class SchoolOut(BaseModel):
    id: str
    name: str
    slug: str
    plan: str
    is_active: bool


class TeacherIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None


class TeacherPatch(BaseModel):
    full_name: str | None = None
    password: str | None = None
    specialization: str | None = None
    home_classroom_id: str | None = None   # classroom this teacher is class teacher of


class TeacherOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    specialization: str | None
    is_active: bool
    created_at: datetime
    home_classroom_id: str | None = None
    home_classroom_name: str | None = None
    home_grade: int | None = None


class SubjectAssignIn(BaseModel):
    classroom_ids: list[str]   # full replacement — send the complete desired set


class SubjectAssignmentOut(BaseModel):
    id: str
    classroom_id: str
    classroom_name: str
    grade: int
    section: str
    subject: str


# ── Timetable schemas ────────────────────────────────────

DAYS = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]  # index 1-6
TIMETABLE_SUBJECTS = SUBJECTS + ["Leisure"]   # Leisure covers free/activity periods


class TimetableSlotIn(BaseModel):
    day: int        # 1=Mon … 6=Sat
    period: int     # 1-8
    subject: str
    classroom_id: str | None = None


class TimetableSlotOut(BaseModel):
    id: str
    day: int
    day_name: str
    period: int
    subject: str
    classroom_id: str | None
    classroom_name: str | None
    start_time: time | None = None
    end_time: time | None = None


class TimetableIn(BaseModel):
    entries: list[TimetableSlotIn]


class TimetableOut(BaseModel):
    entries: list[TimetableSlotOut]


class ClassTimetableSlotIn(BaseModel):
    day: int
    period: int
    subject: str
    teacher_id: str | None = None


class ClassTimetableSlotOut(BaseModel):
    id: str
    day: int
    day_name: str
    period: int
    subject: str
    teacher_id: str | None
    teacher_name: str | None
    start_time: time | None = None
    end_time: time | None = None


class ClassTimetableIn(BaseModel):
    entries: list[ClassTimetableSlotIn]


class ClassTimetableOut(BaseModel):
    entries: list[ClassTimetableSlotOut]


# ── Period (bell-schedule) schemas ───────────────────────


DAY_TYPES = ("weekday", "saturday")


def day_type_for(day_of_week: int) -> str:
    """1=Mon … 6=Sat → schedule variant."""
    return "saturday" if day_of_week == 6 else "weekday"


class PeriodTimeIn(BaseModel):
    period: int
    start_time: time
    end_time: time


class PeriodTimeOut(BaseModel):
    period: int
    start_time: time
    end_time: time


class PeriodTimesIn(BaseModel):
    """Full replacement for both schedules."""
    weekday: list[PeriodTimeIn] = []
    saturday: list[PeriodTimeIn] = []


class PeriodTimesOut(BaseModel):
    weekday: list[PeriodTimeOut] = []
    saturday: list[PeriodTimeOut] = []


# ── Student schemas ───────────────────────────────────────

class SchoolStudentOut(BaseModel):
    id: str
    handle: str
    full_name: str
    grade: int
    section: str
    classroom_id: str | None
    classroom_name: str | None
    parent_email: str | None    # stores parent phone — kept as parent_email for API compat
    parent_phone: str | None = None
    parent_name: str | None = None


class SchoolStudentPatch(BaseModel):
    full_name: str | None = None
    handle: str | None = None
    classroom_id: str | None = None
    parent_name: str | None = None
    parent_phone: str | None = None
    parent_password: str | None = None   # set to reset the parent's login password


class SchoolOnboardIn(BaseModel):
    full_name: str
    handle: str
    classroom_id: str
    parent_phone: str
    parent_name: str | None = None
    parent_password: str


class StudentImportRowError(BaseModel):
    row: int
    reason: str


class StudentImportCredential(BaseModel):
    student_name: str
    parent_email: str
    parent_password: str


class StudentImportResult(BaseModel):
    created: int
    skipped: int
    errors: list[StudentImportRowError]
    credentials: list[StudentImportCredential]


class ClassroomIn(BaseModel):
    name: str
    grade: int
    section: str
    capacity: int = 30
    teacher_id: str | None = None


class ClassroomPatch(BaseModel):
    name: str | None = None
    capacity: int | None = None
    teacher_id: str | None = None


class ClassroomOut(BaseModel):
    id: str
    name: str
    grade: int
    section: str
    capacity: int
    enrolled: int = 0
    available: int = 0
    is_full: bool = False
    teacher_id: str | None
    teacher_name: str | None
    created_at: datetime


# ── School info ──────────────────────────────────────────


@router.get("/me", response_model=SchoolOut)
async def my_school(user: SchoolAdminDep, session: SessionDep) -> SchoolOut:
    from sqlalchemy import func
    tid = assert_tenant(user)
    result = await session.execute(select(Tenant).where(Tenant.id == tid))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(404, "school not found")

    # Idempotent: provision default classrooms if the school has none yet
    # (covers tenants seeded before auto-provisioning was added)
    existing_count = await session.scalar(
        select(func.count()).where(Classroom.tenant_id == tid)
    )
    if not existing_count:
        from backend.api._grades import DEFAULT_GRADES, classroom_name
        for grade in DEFAULT_GRADES:
            cls = Classroom(
                id=uuid.uuid4(),
                tenant_id=tid,
                name=classroom_name(grade, "A"),
                grade=grade,
                section="A",
                capacity=35,
                teacher_id=None,
            )
            session.add(cls)
        await session.flush()

    return SchoolOut(
        id=str(school.id), name=school.name, slug=school.slug,
        plan=school.plan.value, is_active=school.is_active,
    )


# ── Teacher management ───────────────────────────────────


@router.get("/teachers", response_model=list[TeacherOut])
async def list_teachers(user: SchoolAdminDep, session: SessionDep) -> list[TeacherOut]:
    tid = assert_tenant(user)
    result = await session.execute(
        select(User)
        .where(User.tenant_id == tid, User.role == UserRole.teacher)
        .order_by(User.full_name)
    )
    teachers = result.scalars().all()
    cls_rows = (await session.execute(
        select(Classroom).where(
            Classroom.tenant_id == tid, Classroom.teacher_id.is_not(None),
        )
    )).scalars().all()
    home_by_teacher: dict[uuid.UUID, Classroom] = {}
    for c in sorted(cls_rows, key=lambda x: (x.grade, x.section)):
        home_by_teacher.setdefault(c.teacher_id, c)  # first by grade/section
    return [_teacher_out(u, home_by_teacher.get(u.id)) for u in teachers]


@router.post("/teachers", response_model=TeacherOut, status_code=status.HTTP_201_CREATED)
async def create_teacher(
    body: TeacherIn, user: SchoolAdminDep, session: SessionDep
) -> TeacherOut:
    tid = assert_tenant(user)
    existing = await session.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(409, "email already registered")
    teacher = User(
        id=uuid.uuid4(),
        tenant_id=tid,
        email=body.email,
        password_hash=hash_password(body.password),
        role=UserRole.teacher,
        full_name=body.full_name,
    )
    session.add(teacher)
    await session.flush()
    await session.refresh(teacher)
    return _teacher_out(teacher)


@router.patch("/teachers/{teacher_id}", response_model=TeacherOut)
async def edit_teacher(
    teacher_id: str, body: TeacherPatch, user: SchoolAdminDep, session: SessionDep
) -> TeacherOut:
    from backend.core.security import hash_password as _hp
    teacher = await _get_teacher(teacher_id, user, session)
    if body.full_name is not None:
        teacher.full_name = body.full_name
    if body.password:
        teacher.password_hash = _hp(body.password)
    if body.specialization is not None:
        if body.specialization and body.specialization not in SUBJECTS:
            raise HTTPException(400, f"invalid subject. Choose from: {', '.join(SUBJECTS)}")
        teacher.specialization = body.specialization or None

    tid = assert_tenant(user)
    if "home_classroom_id" in body.model_fields_set:
        # Free any classroom this teacher currently leads (one home grade max).
        from sqlalchemy import update as sql_update
        await session.execute(
            sql_update(Classroom)
            .where(Classroom.tenant_id == tid, Classroom.teacher_id == teacher.id)
            .values(teacher_id=None)
        )
        if body.home_classroom_id:
            target = await _get_classroom(body.home_classroom_id, tid, session)
            target.teacher_id = teacher.id

    await session.flush()
    return _teacher_out(teacher, await _home_classroom(session, tid, teacher.id))


@router.patch("/teachers/{teacher_id}/deactivate", response_model=TeacherOut)
async def deactivate_teacher(
    teacher_id: str, user: SchoolAdminDep, session: SessionDep
) -> TeacherOut:
    teacher = await _get_teacher(teacher_id, user, session)
    teacher.is_active = False
    await session.flush()
    return _teacher_out(teacher, await _home_classroom(session, assert_tenant(user), teacher.id))


@router.patch("/teachers/{teacher_id}/activate", response_model=TeacherOut)
async def activate_teacher(
    teacher_id: str, user: SchoolAdminDep, session: SessionDep
) -> TeacherOut:
    teacher = await _get_teacher(teacher_id, user, session)
    teacher.is_active = True
    await session.flush()
    return _teacher_out(teacher, await _home_classroom(session, assert_tenant(user), teacher.id))


@router.delete("/teachers/{teacher_id}", status_code=200)
async def delete_teacher(
    teacher_id: str, user: SchoolAdminDep, session: SessionDep
) -> dict:
    from sqlalchemy import delete as sql_delete
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import update as sql_update
    tid = assert_tenant(user)
    teacher = await _get_teacher(teacher_id, user, session)

    # Unset this teacher from any classroom where they are the home teacher
    await session.execute(
        sql_update(Classroom)
        .where(Classroom.tenant_id == tid, Classroom.teacher_id == teacher.id)
        .values(teacher_id=None)
    )
    # Remove subject assignments
    await session.execute(
        sql_delete(TeacherSubjectAssignment)
        .where(TeacherSubjectAssignment.teacher_id == teacher.id)
    )
    # Delete the user record
    try:
        await session.execute(sql_delete(User).where(User.id == teacher.id))
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            409,
            "Cannot delete: teacher has assessment or upload records. Deactivate instead.",
        )
    return {"deleted": True}


# ── Subject assignment ───────────────────────────────────


@router.get("/subjects", response_model=list[str])
async def list_subjects(_: SchoolAdminDep) -> list[str]:
    return SUBJECTS


@router.get("/teachers/{teacher_id}/assignments", response_model=list[SubjectAssignmentOut])
async def get_teacher_assignments(
    teacher_id: str, user: SchoolAdminDep, session: SessionDep
) -> list[SubjectAssignmentOut]:
    teacher = await _get_teacher(teacher_id, user, session)
    result = await session.execute(
        select(TeacherSubjectAssignment)
        .where(TeacherSubjectAssignment.teacher_id == teacher.id)
        .order_by(TeacherSubjectAssignment.subject)
    )
    assignments = result.scalars().all()
    out = []
    for a in assignments:
        cls = await session.get(Classroom, a.classroom_id)
        if cls:
            out.append(SubjectAssignmentOut(
                id=str(a.id),
                classroom_id=str(a.classroom_id),
                classroom_name=cls.name,
                grade=cls.grade,
                section=cls.section,
                subject=a.subject,
            ))
    return out


@router.put("/teachers/{teacher_id}/assignments", response_model=list[SubjectAssignmentOut])
async def set_teacher_assignments(
    teacher_id: str, body: SubjectAssignIn, user: SchoolAdminDep, session: SessionDep
) -> list[SubjectAssignmentOut]:
    """Replace the full set of classroom assignments for a teacher's subject.
    Sends the complete desired list — backend diffs and adds/removes accordingly.
    """
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)
    teacher = await _get_teacher(teacher_id, user, session)

    if not teacher.specialization:
        raise HTTPException(400, "Set the teacher's specialization before assigning classrooms.")

    # Validate all submitted classroom ids belong to this school
    desired: set[uuid.UUID] = set()
    for cid_str in body.classroom_ids:
        try:
            cid = uuid.UUID(cid_str)
        except ValueError:
            raise HTTPException(400, f"invalid classroom_id: {cid_str}")
        cls_row = await session.scalar(
            select(Classroom).where(Classroom.id == cid, Classroom.tenant_id == tid)
        )
        if not cls_row:
            raise HTTPException(404, f"classroom {cid_str} not found in this school")
        desired.add(cid)

    # Fetch existing assignments
    existing_result = await session.execute(
        select(TeacherSubjectAssignment).where(
            TeacherSubjectAssignment.teacher_id == teacher.id
        )
    )
    existing = {a.classroom_id: a for a in existing_result.scalars().all()}
    existing_ids = set(existing.keys())

    # Remove de-selected classrooms
    to_remove = existing_ids - desired
    if to_remove:
        await session.execute(
            sql_delete(TeacherSubjectAssignment).where(
                TeacherSubjectAssignment.teacher_id == teacher.id,
                TeacherSubjectAssignment.classroom_id.in_(list(to_remove)),
            )
        )

    # Add newly selected classrooms
    to_add = desired - existing_ids
    for cid in to_add:
        session.add(TeacherSubjectAssignment(
            id=uuid.uuid4(),
            tenant_id=tid,
            teacher_id=teacher.id,
            classroom_id=cid,
            subject=teacher.specialization,
        ))

    await session.flush()

    # Return updated assignments
    result = await session.execute(
        select(TeacherSubjectAssignment)
        .where(TeacherSubjectAssignment.teacher_id == teacher.id)
        .order_by(TeacherSubjectAssignment.subject)
    )
    assignments = result.scalars().all()
    out = []
    for a in assignments:
        cls = await session.get(Classroom, a.classroom_id)
        if cls:
            out.append(SubjectAssignmentOut(
                id=str(a.id),
                classroom_id=str(a.classroom_id),
                classroom_name=cls.name,
                grade=cls.grade,
                section=cls.section,
                subject=a.subject,
            ))
    return out


# ── Timetable management ─────────────────────────────────


class GenerateClassroomPlan(BaseModel):
    classroom_id: str
    periods_per_week: int


class GenerateTimetableIn(BaseModel):
    plan: list[GenerateClassroomPlan]


class GenerateTimetableOut(BaseModel):
    entries: list[TimetableSlotOut]
    placed: int
    requested: int
    unplaced: list[dict]   # [{classroom_id, classroom_name, missing}]
    subject: str


@router.post(
    "/teachers/{teacher_id}/timetable/generate",
    response_model=GenerateTimetableOut,
)
async def generate_teacher_timetable(
    teacher_id: str,
    body: GenerateTimetableIn,
    user: SchoolAdminDep,
    session: SessionDep,
) -> GenerateTimetableOut:
    """Smart-schedule a teacher's weekly periods across their assigned classrooms.

    Heuristic placement (no LLM call): spread periods across distinct weekdays
    first, avoid time-slots already held by other teachers in the same
    classroom, never double-book the teacher, and prefer non-adjacent slots in
    the same classroom on the same day. Wipes the teacher's existing timetable
    before writing the new one so both class- and teacher-views stay in sync.
    """
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)
    teacher = await _get_teacher(teacher_id, user, session)

    if not teacher.specialization:
        raise HTTPException(400, "Set the teacher's specialization before generating.")

    subject = teacher.specialization

    # Allowed classrooms = subject assignments ∪ home-grade classroom
    sa_result = await session.execute(
        select(TeacherSubjectAssignment.classroom_id)
        .where(TeacherSubjectAssignment.teacher_id == teacher.id)
    )
    allowed_cids: set[uuid.UUID] = set(sa_result.scalars().all())
    home_cls = await session.scalar(
        select(Classroom).where(
            Classroom.tenant_id == tid, Classroom.teacher_id == teacher.id
        )
    )
    if home_cls:
        allowed_cids.add(home_cls.id)

    # Validate + normalise plan
    plan: dict[uuid.UUID, int] = {}
    for item in body.plan:
        if item.periods_per_week <= 0:
            continue
        try:
            cid = uuid.UUID(item.classroom_id)
        except ValueError:
            raise HTTPException(400, f"invalid classroom_id: {item.classroom_id}")
        if cid not in allowed_cids:
            raise HTTPException(
                400,
                "classroom is not in this teacher's assignments or home grade",
            )
        plan[cid] = item.periods_per_week

    if not plan:
        raise HTTPException(400, "plan must include at least one classroom with periods > 0")

    # Load weekday bell schedule periods (Mon-Fri); skip Saturday for generation
    p_result = await session.execute(
        select(PeriodTime).where(
            PeriodTime.tenant_id == tid, PeriodTime.day_type == "weekday"
        ).order_by(PeriodTime.start_time)
    )
    weekday_periods = [p.period_number for p in p_result.scalars().all()]
    if not weekday_periods:
        raise HTTPException(
            400,
            "Bell schedule (weekday) not set. Configure period times first.",
        )

    days = [1, 2, 3, 4, 5]   # Mon-Fri only

    # Pre-load classroom slots already taken by OTHER teachers so we don't
    # double-book a classroom. We're about to wipe this teacher's rows, so
    # exclude them.
    blocked_by_class: dict[uuid.UUID, set[tuple[int, int]]] = {cid: set() for cid in plan}
    if plan:
        existing_result = await session.execute(
            select(TimetableEntry).where(
                TimetableEntry.classroom_id.in_(list(plan.keys())),
                TimetableEntry.teacher_id != teacher.id,
                TimetableEntry.day_of_week.in_(days),
            )
        )
        for e in existing_result.scalars().all():
            blocked_by_class[e.classroom_id].add((e.day_of_week, e.period_number))

    # Heuristic placement
    teacher_used: set[tuple[int, int]] = set()
    placed_by_class: dict[uuid.UUID, list[tuple[int, int]]] = {cid: [] for cid in plan}

    # Order classrooms by descending requested count → harder ones get first pick
    ordered = sorted(plan.items(), key=lambda kv: -kv[1])

    def slot_score(cid: uuid.UUID, day: int, period: int) -> int:
        """Lower is better. Penalise: same day already used by this class,
        adjacent period to an existing slot of the same class on same day,
        and bunching toward edges of the day."""
        same_day = [pr for (d, pr) in placed_by_class[cid] if d == day]
        score = 0
        score += 5 * len(same_day)                         # avoid same day
        if any(abs(pr - period) <= 1 for pr in same_day):  # avoid back-to-back
            score += 10
        # mild bias to mid-day periods (avoids dumping everything at P1 or last)
        mid = (max(weekday_periods) + min(weekday_periods)) / 2
        score += int(abs(period - mid))
        return score

    # Round-robin passes — one class at a time picks its best remaining slot.
    progress = True
    while progress:
        progress = False
        for cid, want in ordered:
            if len(placed_by_class[cid]) >= want:
                continue
            best: tuple[int, int, int] | None = None  # (score, day, period)
            for d in days:
                for p in weekday_periods:
                    if (d, p) in teacher_used:
                        continue
                    if (d, p) in blocked_by_class[cid]:
                        continue
                    s = slot_score(cid, d, p)
                    if best is None or s < best[0]:
                        best = (s, d, p)
            if best is not None:
                _, d, p = best
                teacher_used.add((d, p))
                placed_by_class[cid].append((d, p))
                progress = True

    # Write entries — wipe teacher rows first to keep both views consistent
    await session.execute(
        sql_delete(TimetableEntry).where(TimetableEntry.teacher_id == teacher.id)
    )
    for cid, slots in placed_by_class.items():
        for (d, p) in slots:
            # Defensive: drop any stale row that still holds this classroom slot
            await session.execute(
                sql_delete(TimetableEntry).where(
                    TimetableEntry.classroom_id == cid,
                    TimetableEntry.day_of_week == d,
                    TimetableEntry.period_number == p,
                )
            )
            session.add(TimetableEntry(
                id=uuid.uuid4(), tenant_id=tid, teacher_id=teacher.id,
                classroom_id=cid, day_of_week=d, period_number=p,
                subject=subject,
            ))
    await session.flush()

    # Build response
    plookup = await _period_lookup(session, tid)
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.teacher_id == teacher.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()
    out_entries = [await _slot_out(e, session, plookup) for e in entries]

    requested_total = sum(plan.values())
    placed_total = sum(len(v) for v in placed_by_class.values())
    unplaced: list[dict] = []
    for cid, want in plan.items():
        got = len(placed_by_class[cid])
        if got < want:
            cls = await session.get(Classroom, cid)
            unplaced.append({
                "classroom_id": str(cid),
                "classroom_name": cls.name if cls else "",
                "missing": want - got,
            })

    return GenerateTimetableOut(
        entries=out_entries,
        placed=placed_total,
        requested=requested_total,
        unplaced=unplaced,
        subject=subject,
    )


@router.get("/teachers/{teacher_id}/timetable", response_model=TimetableOut)
async def get_teacher_timetable(
    teacher_id: str, user: SchoolAdminDep, session: SessionDep
) -> TimetableOut:
    tid = assert_tenant(user)
    teacher = await _get_teacher(teacher_id, user, session)
    plookup = await _period_lookup(session, tid)
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.teacher_id == teacher.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()
    return TimetableOut(entries=[await _slot_out(e, session, plookup) for e in entries])


@router.put("/teachers/{teacher_id}/timetable", response_model=TimetableOut)
async def set_teacher_timetable(
    teacher_id: str, body: TimetableIn, user: SchoolAdminDep, session: SessionDep
) -> TimetableOut:
    """Full replacement — send the complete desired timetable."""
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)
    teacher = await _get_teacher(teacher_id, user, session)

    # Validate entries
    for e in body.entries:
        if not (1 <= e.day <= 6):
            raise HTTPException(400, f"day must be 1-6 (got {e.day})")
        if not (0 <= e.period <= 12):
            raise HTTPException(400, f"period must be 0-12 (got {e.period})")
        if e.subject not in TIMETABLE_SUBJECTS:
            raise HTTPException(400, f"invalid subject '{e.subject}'")

    # Wipe existing timetable for this teacher
    await session.execute(
        sql_delete(TimetableEntry).where(TimetableEntry.teacher_id == teacher.id)
    )

    # Insert new entries
    for e in body.entries:
        cls_id = None
        if e.classroom_id:
            try:
                cls_id = uuid.UUID(e.classroom_id)
            except ValueError:
                raise HTTPException(400, f"invalid classroom_id '{e.classroom_id}'")
            cls_check = await session.scalar(
                select(Classroom).where(Classroom.id == cls_id, Classroom.tenant_id == tid)
            )
            if not cls_check:
                raise HTTPException(404, f"classroom '{e.classroom_id}' not found")
            # A classroom can only have one teacher in a given day/period — drop
            # whoever else held this classroom slot so both views stay coherent.
            await session.execute(
                sql_delete(TimetableEntry).where(
                    TimetableEntry.classroom_id == cls_id,
                    TimetableEntry.day_of_week == e.day,
                    TimetableEntry.period_number == e.period,
                )
            )
        session.add(TimetableEntry(
            id=uuid.uuid4(), tenant_id=tid, teacher_id=teacher.id,
            classroom_id=cls_id, day_of_week=e.day, period_number=e.period,
            subject=e.subject,
        ))

    await session.flush()

    plookup = await _period_lookup(session, tid)
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.teacher_id == teacher.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()
    return TimetableOut(entries=[await _slot_out(e, session, plookup) for e in entries])


# ── Class timetable (same data, classroom-centric view) ──


async def _class_slot_out(
    e: TimetableEntry,
    session,
    period_lookup: dict[tuple[str, int], PeriodTime] | None = None,
) -> ClassTimetableSlotOut:
    teacher_name = None
    if e.teacher_id:
        t = await session.get(User, e.teacher_id)
        teacher_name = (t.full_name or t.email) if t else None
    pt = None
    if period_lookup:
        pt = period_lookup.get((day_type_for(e.day_of_week), e.period_number))
    return ClassTimetableSlotOut(
        id=str(e.id), day=e.day_of_week,
        day_name=DAYS[e.day_of_week] if 1 <= e.day_of_week <= 6 else "?",
        period=e.period_number, subject=e.subject,
        teacher_id=str(e.teacher_id) if e.teacher_id else None,
        teacher_name=teacher_name,
        start_time=pt.start_time if pt else None,
        end_time=pt.end_time if pt else None,
    )


@router.get(
    "/classrooms/{classroom_id}/timetable", response_model=ClassTimetableOut,
)
async def get_class_timetable(
    classroom_id: str, user: SchoolAdminDep, session: SessionDep
) -> ClassTimetableOut:
    tid = assert_tenant(user)
    cls = await _get_classroom(classroom_id, tid, session)
    plookup = await _period_lookup(session, tid)
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.classroom_id == cls.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()
    return ClassTimetableOut(
        entries=[await _class_slot_out(e, session, plookup) for e in entries]
    )


@router.put(
    "/classrooms/{classroom_id}/timetable", response_model=ClassTimetableOut,
)
async def set_class_timetable(
    classroom_id: str, body: ClassTimetableIn,
    user: SchoolAdminDep, session: SessionDep,
) -> ClassTimetableOut:
    """Full replacement for one classroom. Edits here reflect in the teacher
    timetable view (and vice versa) — both read the same timetable_entries."""
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)
    cls = await _get_classroom(classroom_id, tid, session)

    for e in body.entries:
        if not (1 <= e.day <= 6):
            raise HTTPException(400, f"day must be 1-6 (got {e.day})")
        if not (0 <= e.period <= 12):
            raise HTTPException(400, f"period must be 0-12 (got {e.period})")
        if e.subject not in TIMETABLE_SUBJECTS:
            raise HTTPException(400, f"invalid subject '{e.subject}'")

    # Wipe this classroom's existing timetable
    await session.execute(
        sql_delete(TimetableEntry).where(TimetableEntry.classroom_id == cls.id)
    )

    for e in body.entries:
        if not e.teacher_id:
            # No teacher assigned to this slot — skip (nothing to schedule).
            continue
        try:
            t_id = uuid.UUID(e.teacher_id)
        except ValueError:
            raise HTTPException(400, f"invalid teacher_id '{e.teacher_id}'")
        teacher = await session.scalar(
            select(User).where(
                User.id == t_id, User.tenant_id == tid,
                User.role == UserRole.teacher,
            )
        )
        if not teacher:
            raise HTTPException(404, f"teacher '{e.teacher_id}' not found")
        # A teacher can only be in one place at a time — free this teacher's
        # slot if it was held elsewhere.
        await session.execute(
            sql_delete(TimetableEntry).where(
                TimetableEntry.teacher_id == t_id,
                TimetableEntry.day_of_week == e.day,
                TimetableEntry.period_number == e.period,
            )
        )
        session.add(TimetableEntry(
            id=uuid.uuid4(), tenant_id=tid, teacher_id=t_id,
            classroom_id=cls.id, day_of_week=e.day, period_number=e.period,
            subject=e.subject,
        ))

    await session.flush()

    plookup = await _period_lookup(session, tid)
    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.classroom_id == cls.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()
    return ClassTimetableOut(
        entries=[await _class_slot_out(e, session, plookup) for e in entries]
    )


async def _slot_out(
    e: TimetableEntry,
    session,
    period_lookup: dict[tuple[str, int], PeriodTime] | None = None,
) -> TimetableSlotOut:
    cls_name = None
    if e.classroom_id:
        cls = await session.get(Classroom, e.classroom_id)
        cls_name = cls.name if cls else None
    pt = None
    if period_lookup:
        pt = period_lookup.get((day_type_for(e.day_of_week), e.period_number))
    return TimetableSlotOut(
        id=str(e.id), day=e.day_of_week,
        day_name=DAYS[e.day_of_week] if 1 <= e.day_of_week <= 6 else "?",
        period=e.period_number, subject=e.subject,
        classroom_id=str(e.classroom_id) if e.classroom_id else None,
        classroom_name=cls_name,
        start_time=pt.start_time if pt else None,
        end_time=pt.end_time if pt else None,
    )


async def _period_lookup(
    session, tenant_id: uuid.UUID,
) -> dict[tuple[str, int], PeriodTime]:
    rows = (await session.execute(
        select(PeriodTime).where(PeriodTime.tenant_id == tenant_id)
    )).scalars().all()
    return {(p.day_type, p.period_number): p for p in rows}


# ── Period (bell-schedule) endpoints ─────────────────────


def _group_periods(rows: list[PeriodTime]) -> PeriodTimesOut:
    out = PeriodTimesOut(weekday=[], saturday=[])
    for p in sorted(rows, key=lambda x: x.period_number):
        item = PeriodTimeOut(
            period=p.period_number, start_time=p.start_time, end_time=p.end_time,
        )
        if p.day_type == "saturday":
            out.saturday.append(item)
        else:
            out.weekday.append(item)
    return out


def _validate_periods(periods: list[PeriodTimeIn], label: str) -> None:
    seen: set[int] = set()
    for p in periods:
        if not (0 <= p.period <= 12):
            raise HTTPException(400, f"{label} period must be 0-12 (got {p.period})")
        if p.start_time >= p.end_time:
            raise HTTPException(
                400, f"{label} period {p.period}: start must be before end"
            )
        if p.period in seen:
            raise HTTPException(400, f"{label} duplicate period {p.period}")
        seen.add(p.period)


@router.get("/period-times", response_model=PeriodTimesOut)
async def list_period_times(user: SchoolAdminDep, session: SessionDep) -> PeriodTimesOut:
    tid = assert_tenant(user)
    rows = (await session.execute(
        select(PeriodTime).where(PeriodTime.tenant_id == tid)
    )).scalars().all()
    return _group_periods(rows)


@router.put("/period-times", response_model=PeriodTimesOut)
async def set_period_times(
    body: PeriodTimesIn, user: SchoolAdminDep, session: SessionDep
) -> PeriodTimesOut:
    """Full replacement of both the weekday and Saturday bell schedules."""
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)

    _validate_periods(body.weekday, "weekday")
    _validate_periods(body.saturday, "saturday")

    await session.execute(
        sql_delete(PeriodTime).where(PeriodTime.tenant_id == tid)
    )
    for day_type, periods in (("weekday", body.weekday), ("saturday", body.saturday)):
        for p in periods:
            session.add(PeriodTime(
                id=uuid.uuid4(), tenant_id=tid, day_type=day_type,
                period_number=p.period,
                start_time=p.start_time, end_time=p.end_time,
            ))
    await session.flush()

    rows = (await session.execute(
        select(PeriodTime).where(PeriodTime.tenant_id == tid)
    )).scalars().all()
    return _group_periods(rows)


# ── Classroom management ─────────────────────────────────


@router.get("/classrooms", response_model=list[ClassroomOut])
async def list_classrooms(user: SchoolAdminDep, session: SessionDep) -> list[ClassroomOut]:
    tid = assert_tenant(user)
    result = await session.execute(
        select(Classroom).where(Classroom.tenant_id == tid).order_by(Classroom.grade, Classroom.section)
    )
    classrooms = result.scalars().all()
    return [await _classroom_out(c, session) for c in classrooms]


@router.post("/classrooms", response_model=ClassroomOut, status_code=status.HTTP_201_CREATED)
async def create_classroom(
    body: ClassroomIn, user: SchoolAdminDep, session: SessionDep
) -> ClassroomOut:
    tid = assert_tenant(user)
    teacher_id = await _resolve_teacher(body.teacher_id, tid, session)

    cls = Classroom(
        id=uuid.uuid4(),
        tenant_id=tid,
        name=body.name,
        grade=body.grade,
        section=body.section,
        capacity=body.capacity,
        teacher_id=teacher_id,
    )
    session.add(cls)
    await session.flush()
    await session.refresh(cls)
    return await _classroom_out(cls, session)


@router.patch("/classrooms/{classroom_id}", response_model=ClassroomOut)
async def update_classroom(
    classroom_id: str, body: ClassroomPatch, user: SchoolAdminDep, session: SessionDep
) -> ClassroomOut:
    tid = assert_tenant(user)
    cls = await _get_classroom(classroom_id, tid, session)
    if body.name is not None:
        cls.name = body.name
    if body.capacity is not None:
        cls.capacity = body.capacity
    # Use model_fields_set so that an explicit null clears the teacher,
    # while an absent field leaves the teacher unchanged.
    if "teacher_id" in body.model_fields_set:
        cls.teacher_id = await _resolve_teacher(body.teacher_id, tid, session)
    await session.flush()
    return await _classroom_out(cls, session)


@router.delete("/classrooms/{classroom_id}", status_code=200)
async def delete_classroom(
    classroom_id: str, user: SchoolAdminDep, session: SessionDep
) -> dict:
    from sqlalchemy import delete as sql_delete, update as sql_update
    from sqlalchemy.exc import IntegrityError
    from backend.db.models import Student
    tid = assert_tenant(user)
    cls = await _get_classroom(classroom_id, tid, session)

    # Check if students are enrolled — refuse deletion if occupied
    from sqlalchemy import func
    enrolled = await session.scalar(
        select(func.count()).where(Student.classroom_id == cls.id)
    ) or 0
    if enrolled:
        raise HTTPException(
            409,
            f"Cannot delete: {enrolled} student(s) are enrolled in this classroom. "
            "Reassign or remove them first.",
        )
    # Remove subject assignments pointing to this classroom
    await session.execute(
        sql_delete(TeacherSubjectAssignment)
        .where(TeacherSubjectAssignment.classroom_id == cls.id)
    )
    try:
        await session.execute(sql_delete(Classroom).where(Classroom.id == cls.id))
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "Cannot delete: classroom has linked history records.")
    return {"deleted": True}


# ── Student management ───────────────────────────────────


@router.get("/students", response_model=list[SchoolStudentOut])
async def list_school_students(
    user: SchoolAdminDep,
    session: SessionDep,
    classroom_id: str | None = None,
) -> list[SchoolStudentOut]:
    tid = assert_tenant(user)
    stmt = select(Student).where(Student.tenant_id == tid)
    if classroom_id:
        try:
            cid = uuid.UUID(classroom_id)
        except ValueError:
            raise HTTPException(400, "invalid classroom_id")
        stmt = stmt.where(Student.classroom_id == cid)
    result = await session.execute(stmt.order_by(Student.full_name))
    students = result.scalars().all()

    out = []
    for s in students:
        cls_name = None
        if s.classroom_id:
            cls = await session.get(Classroom, s.classroom_id)
            cls_name = cls.name if cls else None
        parent = await _linked_parent(session, s.id)
        out.append(SchoolStudentOut(
            id=str(s.id), handle=s.handle, full_name=s.full_name,
            grade=s.grade, section=s.section,
            classroom_id=str(s.classroom_id) if s.classroom_id else None,
            classroom_name=cls_name, parent_email=s.parent_phone,
            parent_phone=s.parent_phone,
            parent_name=parent.full_name if parent else None,
        ))
    return out


@router.post("/students/onboard", response_model=SchoolStudentOut, status_code=status.HTTP_201_CREATED)
async def onboard_student_admin(
    body: SchoolOnboardIn, user: SchoolAdminDep, session: SessionDep
) -> SchoolStudentOut:
    tid = assert_tenant(user)
    cls = await _get_classroom(body.classroom_id, tid, session)

    dup_roll = await session.scalar(
        select(Student).where(Student.handle == body.handle, Student.tenant_id == tid)
    )
    if dup_roll:
        raise HTTPException(409, "Roll number already in use in this school")

    # Reject if phone belongs to a non-parent account
    if await session.scalar(select(User).where(User.phone == body.parent_phone, User.role != UserRole.parent)):
        raise HTTPException(409, "Phone number is already registered to a non-parent account")

    student = Student(
        id=uuid.uuid4(), tenant_id=tid,
        handle=body.handle, full_name=body.full_name,
        grade=cls.grade, section=cls.section,
        classroom_id=cls.id, parent_phone=body.parent_phone,
    )
    session.add(student)
    await session.flush()

    # Reuse existing parent or create new one. Scope by tenant — a phone that
    # already backs a parent at *another* school can't be silently re-linked
    # to a student here (users.phone is globally unique). The supplied
    # `parent_password` is ignored when reusing an existing parent — we don't
    # change someone's login from a sibling's onboarding form.
    parent = await session.scalar(
        select(User).where(User.phone == body.parent_phone, User.role == UserRole.parent)
    )
    if parent and parent.tenant_id != tid:
        raise HTTPException(
            409,
            "This phone number is already registered to a parent at another "
            "school. Use a different phone or transfer the family first.",
        )
    if not parent:
        from backend.db.repositories.users import normalize_phone
        parent = User(
            id=uuid.uuid4(), tenant_id=tid,
            phone=body.parent_phone,
            phone_digits=normalize_phone(body.parent_phone),
            password_hash=hash_password(body.parent_password),
            role=UserRole.parent, full_name=body.parent_name,
        )
        session.add(parent)
        await session.flush()

    # Dedicated student User (role='student', handle=roll_no). Shares the
    # parent's password initially so the same credential issued at onboarding
    # works for the student-app login too.
    from backend.db.repositories.users import ensure_student_user, HandleAlreadyTaken
    try:
        await ensure_student_user(
            session, student=student, password_hash=parent.password_hash,
        )
    except HandleAlreadyTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Link (idempotent)
    if not await session.scalar(
        select(ParentStudent).where(
            ParentStudent.parent_id == parent.id,
            ParentStudent.student_id == student.id,
        )
    ):
        session.add(ParentStudent(id=uuid.uuid4(), parent_id=parent.id, student_id=student.id))
        await session.flush()
    await session.refresh(student)

    return SchoolStudentOut(
        id=str(student.id), handle=student.handle, full_name=student.full_name,
        grade=student.grade, section=student.section,
        classroom_id=str(student.classroom_id), classroom_name=cls.name,
        parent_email=student.parent_phone,
    )


@router.post("/students/import", response_model=StudentImportResult)
async def import_students_admin(
    user: SchoolAdminDep,
    session: SessionDep,
    file: UploadFile = File(...),
    classroom_id: str = Form(...),
) -> StudentImportResult:
    import csv
    import io
    import secrets
    import httpx
    from backend.core.config import get_settings

    tid = assert_tenant(user)
    cls = await _get_classroom(classroom_id, tid, session)

    raw = await file.read()
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.converter_url}/convert",
                files={"file": (file.filename or "upload", raw, file.content_type or "application/octet-stream")},
            )
        if not resp.is_success:
            raise HTTPException(422, f"Converter error: {resp.text[:200]}")
        sheets = resp.json().get("sheets", [])
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Converter service unavailable: {exc}") from exc

    if not sheets:
        raise HTTPException(422, "No data found in the uploaded file.")

    _ALIASES: dict[str, list[str]] = {
        "full_name":       ["full_name", "name", "student_name", "student name", "fullname"],
        "roll_no":         ["roll_no", "roll", "handle", "roll no", "roll_number", "admission_no", "id"],
        "parent_phone":    ["parent_phone", "phone", "mobile", "parent phone", "guardian_phone", "contact"],
        "parent_name":     [
            "parent_name", "parent name", "parents_name", "parents name",
            "parent", "parent/guardian", "guardian", "guardian_name",
            "guardian name", "name of parent", "father_name", "father name",
            "mother_name", "mother name",
        ],
        "parent_password": ["parent_password", "password", "parent password"],
    }

    import re

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

    def _find(headers: list[str], field: str) -> str | None:
        norm = {_norm(h): h for h in headers}
        for alias in _ALIASES[field]:
            if _norm(alias) in norm:
                return norm[_norm(alias)]
        who = ("parent", "guardian", "father", "mother")
        if field == "parent_name":
            for n, orig in norm.items():
                if "name" in n.split() and any(k in n for k in who):
                    return orig
        elif field == "parent_phone":
            for n, orig in norm.items():
                if any(k in n for k in ("phone", "mobile", "contact", "whatsapp", "number")) \
                        and (any(k in n for k in who) or n in ("phone", "mobile", "contact")):
                    return orig
        elif field == "full_name":
            for n, orig in norm.items():
                if "name" in n.split() and not any(k in n for k in who):
                    return orig
        return None

    reader = csv.DictReader(io.StringIO(sheets[0]["csv"]))
    headers = list(reader.fieldnames or [])
    col_name  = _find(headers, "full_name")
    col_roll  = _find(headers, "roll_no")
    col_phone = _find(headers, "parent_phone")
    col_pname = _find(headers, "parent_name")
    col_ppass = _find(headers, "parent_password")

    if not col_name or not col_roll or not col_phone:
        missing = [f for f, c in [("full_name", col_name), ("roll_no", col_roll), ("parent_phone", col_phone)] if not c]
        raise HTTPException(422, f"Required column(s) not found: {', '.join(missing)}. Expected: full_name, roll_no, parent_phone")

    created = 0
    skipped = 0
    errors: list[StudentImportRowError] = []
    credentials: list[StudentImportCredential] = []

    if not col_pname:
        errors.append(StudentImportRowError(
            row=0,
            reason=(
                "No parent-name column detected — parent names were not imported. "
                f"Columns found: {headers}. Add a 'parent_name' column."
            ),
        ))

    for idx, row in enumerate(reader, start=2):
        name  = (row.get(col_name)  or "").strip()
        roll  = (row.get(col_roll)  or "").strip()
        phone = (row.get(col_phone) or "").strip()
        pname = (row.get(col_pname) or "").strip() if col_pname else None
        ppass = (row.get(col_ppass) or "").strip() if col_ppass else None

        if not name or not roll or not phone:
            errors.append(StudentImportRowError(row=idx, reason="Missing required value(s)"))
            continue

        # Roll number conflict → always skip
        if await session.scalar(select(Student).where(Student.handle == roll, Student.tenant_id == tid)):
            skipped += 1
            errors.append(StudentImportRowError(row=idx, reason=f"Roll no '{roll}' already exists — skipped"))
            continue

        # Phone registered to non-parent → conflict, skip
        if await session.scalar(select(User).where(User.phone == phone, User.role != UserRole.parent)):
            skipped += 1
            errors.append(StudentImportRowError(row=idx, reason=f"Phone '{phone}' is used by a non-parent account — skipped"))
            continue

        plain_pw = ppass or secrets.token_urlsafe(8)
        student = Student(
            id=uuid.uuid4(), tenant_id=tid, handle=roll, full_name=name,
            grade=cls.grade, section=cls.section, classroom_id=cls.id, parent_phone=phone,
        )
        session.add(student)
        await session.flush()

        # Reuse existing parent or create new one
        parent_user = await session.scalar(
            select(User).where(User.phone == phone, User.role == UserRole.parent)
        )
        is_new_parent = parent_user is None
        if not parent_user:
            from backend.db.repositories.users import normalize_phone
            parent_user = User(
                id=uuid.uuid4(), tenant_id=tid, phone=phone,
                phone_digits=normalize_phone(phone),
                password_hash=hash_password(plain_pw),
                role=UserRole.parent, full_name=pname or None,
            )
            session.add(parent_user)
            await session.flush()
        elif pname:
            # Existing parent (e.g. sibling row) — keep the imported name.
            parent_user.full_name = pname

        # Dedicated student User (role='student', handle=roll_no). Shares
        # the parent's password initially.
        from backend.db.repositories.users import ensure_student_user, HandleAlreadyTaken
        try:
            await ensure_student_user(
                session, student=student, password_hash=parent_user.password_hash,
            )
        except HandleAlreadyTaken as exc:
            # Roll the half-created student back so the batch keeps going.
            await session.delete(student)
            await session.flush()
            skipped += 1
            errors.append(StudentImportRowError(row=idx, reason=str(exc)))
            continue

        # Link (idempotent)
        if not await session.scalar(
            select(ParentStudent).where(
                ParentStudent.parent_id == parent_user.id,
                ParentStudent.student_id == student.id,
            )
        ):
            session.add(ParentStudent(id=uuid.uuid4(), parent_id=parent_user.id, student_id=student.id))
            await session.flush()

        created += 1
        if is_new_parent:
            credentials.append(StudentImportCredential(student_name=name, parent_email=phone, parent_password=plain_pw))

    return StudentImportResult(created=created, skipped=skipped, errors=errors, credentials=credentials)


@router.patch("/students/{student_id}", response_model=SchoolStudentOut)
async def update_student_admin(
    student_id: str, body: SchoolStudentPatch,
    user: SchoolAdminDep, session: SessionDep,
) -> SchoolStudentOut:
    """Rename a student, change roll number, classroom, or parent name/phone."""
    from sqlalchemy import update as sql_update
    tid = assert_tenant(user)
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise HTTPException(400, "invalid student_id")
    student = await session.scalar(
        select(Student).where(Student.id == sid, Student.tenant_id == tid)
    )
    if not student:
        raise HTTPException(404, "student not found")

    if body.full_name is not None:
        student.full_name = body.full_name

    if body.handle is not None and body.handle != student.handle:
        dup = await session.scalar(
            select(Student).where(
                Student.handle == body.handle,
                Student.tenant_id == tid,
                Student.id != sid,
            )
        )
        if dup:
            raise HTTPException(409, "Roll number already in use in this school")
        student.handle = body.handle

    cls_name = None
    if body.classroom_id is not None:
        cls = await _get_classroom(body.classroom_id, tid, session)
        student.classroom_id = cls.id
        student.grade = cls.grade        # keep grade/section in sync with the classroom
        student.section = cls.section
        cls_name = cls.name
    elif student.classroom_id:
        existing_cls = await session.get(Classroom, student.classroom_id)
        cls_name = existing_cls.name if existing_cls else None

    # ── Parent details ───────────────────────────────────────
    parent = await _linked_parent(session, student.id)
    new_phone = body.parent_phone.strip() if body.parent_phone else None

    if new_phone and (not parent or new_phone != parent.phone):
        clash = await session.scalar(
            select(User).where(
                User.phone == new_phone,
                User.role != UserRole.parent,
            )
        )
        if clash:
            raise HTTPException(409, "Phone number is already registered to a non-parent account")

    new_pw = body.parent_password.strip() if body.parent_password else None

    if parent:
        if body.parent_name is not None:
            parent.full_name = body.parent_name
        if new_pw:
            parent.password_hash = hash_password(new_pw)
        if new_phone and new_phone != parent.phone:
            existing_parent = await session.scalar(
                select(User).where(
                    User.phone == new_phone,
                    User.role == UserRole.parent,
                    User.id != parent.id,
                )
            )
            if existing_parent:
                # Re-point the link to the parent that already owns this phone.
                await session.execute(
                    sql_update(ParentStudent)
                    .where(ParentStudent.student_id == student.id,
                           ParentStudent.parent_id == parent.id)
                    .values(parent_id=existing_parent.id)
                )
                if body.parent_name is not None:
                    existing_parent.full_name = body.parent_name
                if new_pw:
                    existing_parent.password_hash = hash_password(new_pw)
            else:
                from backend.db.repositories.users import normalize_phone
                parent.phone = new_phone
                parent.phone_digits = normalize_phone(new_phone)
        student.parent_phone = new_phone or student.parent_phone
    elif new_phone:
        # No parent linked yet — create one. Password defaults to the phone
        # number unless an explicit one was provided.
        new_parent = User(
            id=uuid.uuid4(), tenant_id=tid, phone=new_phone,
            password_hash=hash_password(new_pw or new_phone),
            role=UserRole.parent, full_name=body.parent_name,
        )
        session.add(new_parent)
        await session.flush()
        session.add(ParentStudent(
            id=uuid.uuid4(), parent_id=new_parent.id, student_id=student.id,
        ))
        student.parent_phone = new_phone

    await session.flush()
    await session.refresh(student)
    final_parent = await _linked_parent(session, student.id)

    return SchoolStudentOut(
        id=str(student.id), handle=student.handle, full_name=student.full_name,
        grade=student.grade, section=student.section,
        classroom_id=str(student.classroom_id) if student.classroom_id else None,
        classroom_name=cls_name, parent_email=student.parent_phone,
        parent_phone=student.parent_phone,
        parent_name=final_parent.full_name if final_parent else None,
    )


@router.delete("/students/{student_id}", status_code=200)
async def delete_student_admin(
    student_id: str, user: SchoolAdminDep, session: SessionDep
) -> dict:
    from sqlalchemy import delete as sql_delete
    from sqlalchemy.exc import IntegrityError
    tid = assert_tenant(user)
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise HTTPException(400, "invalid student_id")
    student = await session.scalar(
        select(Student).where(Student.id == sid, Student.tenant_id == tid)
    )
    if not student:
        raise HTTPException(404, "student not found")

    # Capture linked parents *and* the dedicated student User before deleting.
    # The parent_students rows cascade with the student; the students.user_id
    # FK is ON DELETE SET NULL so the User row would otherwise be orphaned
    # and keep its `handle` reserved — blocking a re-onboard with the same
    # roll number.
    parent_ids = set((await session.execute(
        select(ParentStudent.parent_id).where(ParentStudent.student_id == sid)
    )).scalars().all())
    student_user_id = student.user_id

    try:
        await session.execute(sql_delete(Student).where(Student.id == sid))
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "Cannot delete: student has linked assessment data.")

    if student_user_id is not None:
        # Savepoint so a FK conflict on the user delete (notifications,
        # conversations, …) rolls back only this delete, not the student
        # delete that already succeeded.
        try:
            async with session.begin_nested():
                await session.execute(sql_delete(User).where(User.id == student_user_id))
        except IntegrityError:
            # User row stays in place. The handle remains reserved but the
            # account is no longer reachable. Admin can rename + retry.
            pass

    await _delete_orphan_parents(session, parent_ids)
    return {"deleted": True}


# ── Helpers ──────────────────────────────────────────────


async def _delete_orphan_parents(session, parent_ids) -> None:
    """Delete parent accounts that no longer have any linked students.

    Parents are shared across siblings, so a parent is only removed once their
    last child is gone. If the parent still has other linked records that block
    deletion (notifications, conversations, …) the account is left in place
    rather than failing the student deletion.
    """
    from sqlalchemy import delete as sql_delete, func
    from sqlalchemy.exc import IntegrityError
    for pid in parent_ids:
        remaining = await session.scalar(
            select(func.count()).select_from(ParentStudent)
            .where(ParentStudent.parent_id == pid)
        )
        if remaining:
            continue
        parent = await session.get(User, pid)
        if not parent or parent.role is not UserRole.parent:
            continue
        try:
            async with session.begin_nested():
                await session.execute(sql_delete(User).where(User.id == pid))
        except IntegrityError:
            pass


async def _linked_parent(session, student_id: uuid.UUID) -> User | None:
    """The first parent User linked to a student via ParentStudent, if any."""
    link = await session.scalar(
        select(ParentStudent).where(ParentStudent.student_id == student_id)
    )
    if not link:
        return None
    return await session.get(User, link.parent_id)


def _teacher_out(u: User, home: Classroom | None = None) -> TeacherOut:
    return TeacherOut(
        id=str(u.id), email=u.email, full_name=u.full_name,
        specialization=u.specialization,
        is_active=u.is_active, created_at=u.created_at,
        home_classroom_id=str(home.id) if home else None,
        home_classroom_name=home.name if home else None,
        home_grade=home.grade if home else None,
    )


async def _home_classroom(session, tenant_id: uuid.UUID, teacher_id: uuid.UUID) -> Classroom | None:
    """The classroom this teacher is the class teacher of, if any."""
    return await session.scalar(
        select(Classroom).where(
            Classroom.tenant_id == tenant_id,
            Classroom.teacher_id == teacher_id,
        ).order_by(Classroom.grade, Classroom.section).limit(1)
    )


async def _classroom_out(cls: Classroom, session: object) -> ClassroomOut:
    from sqlalchemy import func
    from backend.db.models import Student
    enrolled = await session.scalar(  # type: ignore[union-attr]
        select(func.count()).where(Student.classroom_id == cls.id)
    ) or 0
    teacher_name = None
    if cls.teacher_id:
        t = await session.get(User, cls.teacher_id)  # type: ignore[union-attr]
        teacher_name = t.full_name if t else None
    available = max(0, cls.capacity - enrolled)
    return ClassroomOut(
        id=str(cls.id), name=cls.name, grade=cls.grade, section=cls.section,
        capacity=cls.capacity, enrolled=enrolled, available=available,
        is_full=available == 0,
        teacher_id=str(cls.teacher_id) if cls.teacher_id else None,
        teacher_name=teacher_name, created_at=cls.created_at,
    )


async def _get_teacher(teacher_id: str, user: User, session: object) -> User:
    tid = assert_tenant(user)
    try:
        uid = uuid.UUID(teacher_id)
    except ValueError:
        raise HTTPException(400, "invalid teacher id")
    result = await session.execute(  # type: ignore[union-attr]
        select(User).where(User.id == uid, User.tenant_id == tid, User.role == UserRole.teacher)
    )
    teacher = result.scalar_one_or_none()
    if not teacher:
        raise HTTPException(404, "teacher not found")
    return teacher


async def _get_classroom(classroom_id: str, tenant_id: uuid.UUID, session: object) -> Classroom:
    try:
        cid = uuid.UUID(classroom_id)
    except ValueError:
        raise HTTPException(400, "invalid classroom id")
    result = await session.execute(  # type: ignore[union-attr]
        select(Classroom).where(Classroom.id == cid, Classroom.tenant_id == tenant_id)
    )
    cls = result.scalar_one_or_none()
    if not cls:
        raise HTTPException(404, "classroom not found")
    return cls


async def _resolve_teacher(
    teacher_id: str | None, tenant_id: uuid.UUID, session: object
) -> uuid.UUID | None:
    if not teacher_id:
        return None
    try:
        uid = uuid.UUID(teacher_id)
    except ValueError:
        raise HTTPException(400, "invalid teacher_id")
    result = await session.execute(  # type: ignore[union-attr]
        select(User).where(
            User.id == uid, User.tenant_id == tenant_id, User.role == UserRole.teacher
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(404, "teacher not found in this school")
    return uid
