"""
Teacher management endpoints — create students, assign parents, mark attendance.
Students are entities of the SCHOOL (tenant), managed by the teacher.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.core.security import hash_password
from backend.db.models import (
    Assignment, AssignmentQuestion, AssignmentResponse, AssignmentStatus,
    Attendance, AttendanceStatus,
    Classroom, ParentStudent, PeriodTime, Student, TeacherSubjectAssignment,
    TimetableEntry, User, UserRole,
)
from backend.db.repositories.attendance import AttendanceRepository
from backend.db.repositories.students import StudentRepository

router = APIRouter(prefix="/teacher", tags=["teacher"])
TeacherDep = Annotated[User, Depends(require_roles(UserRole.teacher))]


# ── Schemas ──────────────────────────────────────────────


class ClassroomOut(BaseModel):
    id: str
    name: str
    grade: int
    section: str
    student_count: int
    is_home_grade: bool = False
    subject_taught: str | None = None


class StudentIn(BaseModel):
    handle: str
    full_name: str
    grade: int
    section: str
    classroom_id: str | None = None
    parent_email: EmailStr | None = None


class StudentOut(BaseModel):
    id: str
    handle: str
    full_name: str
    grade: int
    section: str
    classroom_id: str | None
    classroom_name: str | None
    parent_phone: str | None
    parent_name: str | None = None


class StudentPatch(BaseModel):
    full_name: str | None = None
    handle: str | None = None
    classroom_id: str | None = None
    parent_name: str | None = None
    parent_phone: str | None = None
    parent_password: str | None = None   # set to reset the parent's login password


class ParentIn(BaseModel):
    phone: str
    password: str
    full_name: str | None = None


class ParentOut(BaseModel):
    id: str
    phone: str
    full_name: str | None


class OnboardIn(BaseModel):
    """Create a student and parent account in one atomic call."""
    full_name: str
    handle: str                      # roll number — unique per school
    classroom_id: str                # must be one of the teacher's classrooms
    parent_phone: str
    parent_name: str | None = None
    parent_password: str


class OnboardOut(BaseModel):
    student: StudentOut
    parent: ParentOut


class AttendanceIn(BaseModel):
    student_id: str
    date: date
    status: AttendanceStatus


class BulkAttendanceIn(BaseModel):
    date: date
    records: list[dict]   # [{student_id, status}]


class AttendanceOut(BaseModel):
    saved: int
    skipped: int = 0
    skipped_student_ids: list[str] = []


class AttendanceRecordOut(BaseModel):
    student_id: str
    student_name: str
    status: str | None   # None = not yet marked


class AttendanceDayOut(BaseModel):
    date: date
    records: list[AttendanceRecordOut]


class ImportRowError(BaseModel):
    row: int
    reason: str


class ImportCredential(BaseModel):
    student_name: str
    parent_email: str
    parent_password: str   # plaintext — only returned once so teacher can share


class ImportResultOut(BaseModel):
    created: int
    skipped: int
    errors: list[ImportRowError]
    credentials: list[ImportCredential]


# ── Classrooms ───────────────────────────────────────────


@router.get("/classrooms", response_model=list[ClassroomOut])
async def my_classrooms(user: TeacherDep, session: SessionDep) -> list[ClassroomOut]:
    """All classrooms associated with this teacher (home grade + subject assignments)."""
    tid = assert_tenant(user)
    from sqlalchemy import func

    home_result = await session.execute(
        select(Classroom)
        .where(Classroom.tenant_id == tid, Classroom.teacher_id == user.id)
        .order_by(Classroom.grade, Classroom.section)
    )
    home_classrooms: dict[uuid.UUID, Classroom] = {
        cls.id: cls for cls in home_result.scalars().all()
    }

    sa_result = await session.execute(
        select(TeacherSubjectAssignment)
        .where(TeacherSubjectAssignment.teacher_id == user.id)
    )
    subject_cls_map: dict[uuid.UUID, str] = {}
    for sa_rec in sa_result.scalars().all():
        if sa_rec.classroom_id not in home_classrooms:
            subject_cls_map[sa_rec.classroom_id] = sa_rec.subject

    subject_classrooms: dict[uuid.UUID, Classroom] = {}
    if subject_cls_map:
        sub_result = await session.execute(
            select(Classroom)
            .where(Classroom.id.in_(list(subject_cls_map.keys())))
            .order_by(Classroom.grade, Classroom.section)
        )
        subject_classrooms = {cls.id: cls for cls in sub_result.scalars().all()}

    out = []
    for cls in sorted(home_classrooms.values(), key=lambda c: (c.grade, c.section)):
        count = await session.scalar(
            select(func.count()).where(Student.classroom_id == cls.id)
        ) or 0
        out.append(ClassroomOut(
            id=str(cls.id), name=cls.name,
            grade=cls.grade, section=cls.section, student_count=count,
            is_home_grade=True, subject_taught=None,
        ))

    for cls in sorted(subject_classrooms.values(), key=lambda c: (c.grade, c.section)):
        count = await session.scalar(
            select(func.count()).where(Student.classroom_id == cls.id)
        ) or 0
        out.append(ClassroomOut(
            id=str(cls.id), name=cls.name,
            grade=cls.grade, section=cls.section, student_count=count,
            is_home_grade=False, subject_taught=subject_cls_map.get(cls.id),
        ))

    return out


# ── Students ─────────────────────────────────────────────


async def _teacher_classroom_ids(
    session, tenant_id: uuid.UUID, teacher_id: uuid.UUID
) -> set[uuid.UUID]:
    """Every classroom a teacher is associated with: their home grade
    (class teacher) PLUS classrooms they teach their specialization in."""
    home = await session.execute(
        select(Classroom.id).where(
            Classroom.tenant_id == tenant_id, Classroom.teacher_id == teacher_id
        )
    )
    ids = set(home.scalars().all())
    sub = await session.execute(
        select(TeacherSubjectAssignment.classroom_id)
        .where(TeacherSubjectAssignment.teacher_id == teacher_id)
    )
    ids.update(sub.scalars().all())
    return ids


async def _teacher_home_classroom_ids(
    session, tenant_id: uuid.UUID, teacher_id: uuid.UUID
) -> set[uuid.UUID]:
    """Only the home-grade classrooms where this teacher is the class teacher."""
    result = await session.execute(
        select(Classroom.id).where(
            Classroom.tenant_id == tenant_id, Classroom.teacher_id == teacher_id
        )
    )
    return set(result.scalars().all())


@router.get("/students", response_model=list[StudentOut])
async def my_students(
    user: TeacherDep,
    session: SessionDep,
    classroom_id: str | None = None,
) -> list[StudentOut]:
    """All students in this teacher's classrooms (home grade + subject classes)."""
    tid = assert_tenant(user)

    my_cls_ids = {str(cid) for cid in await _teacher_classroom_ids(session, tid, user.id)}

    if not my_cls_ids:
        return []

    stmt = select(Student).where(Student.tenant_id == tid)
    if classroom_id:
        if classroom_id not in my_cls_ids:
            raise HTTPException(403, "not your classroom")
        stmt = stmt.where(Student.classroom_id == uuid.UUID(classroom_id))
    else:
        stmt = stmt.where(Student.classroom_id.in_([uuid.UUID(c) for c in my_cls_ids]))

    result = await session.execute(stmt.order_by(Student.full_name))
    students = result.scalars().all()
    return [await _student_out(s, session) for s in students]


@router.post("/students", response_model=StudentOut, status_code=status.HTTP_201_CREATED)
async def create_student(
    body: StudentIn, user: TeacherDep, session: SessionDep
) -> StudentOut:
    tid = assert_tenant(user)

    # Validate classroom belongs to this teacher
    classroom_id: uuid.UUID | None = None
    if body.classroom_id:
        cls = await _get_my_classroom(body.classroom_id, user.id, tid, session)
        classroom_id = cls.id

    repo = StudentRepository(session)
    rows = await repo.upsert_many(
        [{
            "handle": body.handle,
            "full_name": body.full_name,
            "grade": body.grade,
            "section": body.section,
            "parent_email": str(body.parent_email) if body.parent_email else None,
        }],
        tid,
    )

    # Fetch the created student
    student = await repo.get_by_handle(body.handle, tid)
    if not student:
        raise HTTPException(500, "student creation failed")

    # Assign classroom
    if classroom_id:
        student.classroom_id = classroom_id
        await session.flush()

    return await _student_out(student, session)


# ── Timetable (view own) ─────────────────────────────────


def _day_type_for(day_of_week: int) -> str:
    """1=Mon … 6=Sat → bell-schedule variant."""
    return "saturday" if day_of_week == 6 else "weekday"


@router.get("/timetable")
async def my_timetable(user: TeacherDep, session: SessionDep) -> dict:
    tid = assert_tenant(user)
    # Bell schedule for this school, keyed by (day_type, period_number)
    p_rows = (await session.execute(
        select(PeriodTime).where(PeriodTime.tenant_id == tid)
    )).scalars().all()
    period_lookup = {(p.day_type, p.period_number): p for p in p_rows}

    result = await session.execute(
        select(TimetableEntry)
        .where(TimetableEntry.teacher_id == user.id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.period_number)
    )
    entries = result.scalars().all()

    out = []
    today_dow = date.today().isoweekday()  # 1=Mon … 7=Sun
    today_list: list[dict] = []

    for e in entries:
        cls_name = None
        if e.classroom_id:
            cls = await session.get(Classroom, e.classroom_id)
            cls_name = cls.name if cls else None
        pt = period_lookup.get((_day_type_for(e.day_of_week), e.period_number))
        item = {
            "id": str(e.id),
            "day": e.day_of_week,
            "period": e.period_number,
            "subject": e.subject,
            "classroom_id": str(e.classroom_id) if e.classroom_id else None,
            "classroom_name": cls_name,
            "start_time": pt.start_time.isoformat() if pt else None,
            "end_time": pt.end_time.isoformat() if pt else None,
        }
        out.append(item)
        if e.day_of_week == today_dow:
            today_list.append(item)

    today_list.sort(key=lambda x: (x.get("start_time") or "", x["period"]))

    def _sched(day_type: str) -> list[dict]:
        return [
            {
                "period": p.period_number,
                "start_time": p.start_time.isoformat(),
                "end_time": p.end_time.isoformat(),
            }
            for p in sorted(
                (r for r in p_rows if r.day_type == day_type),
                key=lambda x: x.start_time,
            )
        ]

    return {
        "entries": out,
        "today": today_list,
        "periods": {"weekday": _sched("weekday"), "saturday": _sched("saturday")},
    }


# ── Onboard (student + parent in one call) ───────────────


@router.post("/students/onboard", response_model=OnboardOut, status_code=status.HTTP_201_CREATED)
async def onboard_student(
    body: OnboardIn, user: TeacherDep, session: SessionDep
) -> OnboardOut:
    tid = assert_tenant(user)

    cls = await _get_my_classroom(body.classroom_id, user.id, tid, session)

    # Guard: roll number must be unique within the school
    if await session.scalar(select(Student).where(Student.handle == body.handle, Student.tenant_id == tid)):
        raise HTTPException(409, "roll number already in use in this school")

    student = Student(
        id=uuid.uuid4(), tenant_id=tid,
        handle=body.handle, full_name=body.full_name,
        grade=cls.grade, section=cls.section,
        classroom_id=cls.id, parent_phone=body.parent_phone,
    )
    session.add(student)
    await session.flush()

    # Reuse existing parent account or create a new one
    parent = await _find_or_create_parent(
        phone=body.parent_phone,
        password=body.parent_password,
        full_name=body.parent_name,
        tenant_id=tid,
        session=session,
    )

    # Dedicated student User (role='student', handle=roll_no). Shares the
    # parent's password initially so the student can log in with the same
    # credentials they were given. They can change it independently later.
    from backend.db.repositories.users import ensure_student_user, HandleAlreadyTaken
    try:
        await ensure_student_user(
            session, student=student, password_hash=parent.password_hash,
        )
    except HandleAlreadyTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await _link_parent_student(parent.id, student.id, session)
    await session.refresh(student)
    await session.refresh(parent)

    return OnboardOut(
        student=await _student_out(student, session),
        parent=ParentOut(id=str(parent.id), phone=parent.phone or "", full_name=parent.full_name),
    )


# ── Bulk import from Excel ───────────────────────────────


@router.post("/students/import", response_model=ImportResultOut)
async def import_students(
    user: TeacherDep,
    session: SessionDep,
    file: UploadFile = File(...),
    classroom_id: str = Form(...),
) -> ImportResultOut:
    """Upload an Excel/CSV file and bulk-create students + parent accounts.

    Expected columns (case-insensitive, any order):
      full_name | roll_no | parent_email | parent_name (opt) | parent_password (opt)
    """
    import csv
    import io
    import secrets
    import httpx
    from backend.core.config import get_settings

    tid = assert_tenant(user)
    cls = await _get_my_classroom(classroom_id, user.id, tid, session)

    # ── 1. Convert file via converter microservice ────────
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

    csv_text = sheets[0]["csv"]   # use first sheet

    # ── 2. Parse CSV with flexible column matching ────────
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
        # 1. exact (normalised) alias match
        for alias in _ALIASES[field]:
            if _norm(alias) in norm:
                return norm[_norm(alias)]
        # 2. heuristic fallback — tolerate arbitrary real-world headers
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

    reader = csv.DictReader(io.StringIO(csv_text))
    headers = list(reader.fieldnames or [])

    col_name   = _find(headers, "full_name")
    col_roll   = _find(headers, "roll_no")
    col_phone  = _find(headers, "parent_phone")
    col_pname  = _find(headers, "parent_name")
    col_ppass  = _find(headers, "parent_password")

    if not col_name or not col_roll or not col_phone:
        missing = [f for f, c in [("full_name", col_name), ("roll_no", col_roll), ("parent_phone", col_phone)] if not c]
        raise HTTPException(
            422,
            f"Required column(s) not found: {', '.join(missing)}. "
            "Expected headers: full_name, roll_no, parent_phone",
        )

    # ── 3. Process rows ───────────────────────────────────
    created = 0
    skipped = 0
    errors: list[ImportRowError] = []
    credentials: list[ImportCredential] = []

    if not col_pname:
        errors.append(ImportRowError(
            row=0,
            reason=(
                "No parent-name column detected — parent names were not imported. "
                f"Columns found: {headers}. Add a 'parent_name' column."
            ),
        ))

    rows = list(reader)
    for idx, row in enumerate(rows, start=2):
        name  = (row.get(col_name)  or "").strip()
        roll  = (row.get(col_roll)  or "").strip()
        phone = (row.get(col_phone) or "").strip()
        pname = (row.get(col_pname) or "").strip() if col_pname else None
        ppass = (row.get(col_ppass) or "").strip() if col_ppass else None

        if not name or not roll or not phone:
            errors.append(ImportRowError(row=idx, reason="Missing required value(s): name, roll_no, or parent_phone"))
            continue

        # Roll number conflict → always skip
        if await session.scalar(select(Student).where(Student.handle == roll, Student.tenant_id == tid)):
            skipped += 1
            errors.append(ImportRowError(row=idx, reason=f"Roll no '{roll}' already exists — skipped"))
            continue

        # Phone registered to non-parent → conflict, skip
        if await session.scalar(select(User).where(User.phone == phone, User.role != UserRole.parent)):
            skipped += 1
            errors.append(ImportRowError(row=idx, reason=f"Phone '{phone}' is used by a non-parent account — skipped"))
            continue

        plain_password = ppass or secrets.token_urlsafe(8)

        student = Student(
            id=uuid.uuid4(), tenant_id=tid, handle=roll, full_name=name,
            grade=cls.grade, section=cls.section, classroom_id=cls.id, parent_phone=phone,
        )
        session.add(student)
        await session.flush()

        # Reuse existing parent account, or create a new one
        parent = await _find_or_create_parent(
            phone=phone, password=plain_password,
            full_name=pname or None, tenant_id=tid, session=session,
        )
        # _find_or_create_parent never updates an *existing* parent, so apply
        # the name from the sheet here — otherwise sibling rows / pre-existing
        # parents keep losing the imported name.
        if pname:
            parent.full_name = pname
        is_new_parent = not await session.scalar(
            select(ParentStudent).where(ParentStudent.parent_id == parent.id)
        )
        from backend.db.repositories.users import ensure_student_user, HandleAlreadyTaken
        try:
            await ensure_student_user(
                session, student=student, password_hash=parent.password_hash,
            )
        except HandleAlreadyTaken as exc:
            # Roll the half-created student back so the batch can continue.
            # session is request-scoped; deleting + flushing leaves the rest
            # of the transaction intact (no global rollback).
            await session.delete(student)
            await session.flush()
            skipped += 1
            errors.append(ImportRowError(row=idx, reason=str(exc)))
            continue
        await _link_parent_student(parent.id, student.id, session)
        created += 1

        if is_new_parent:
            credentials.append(ImportCredential(
                student_name=name,
                parent_email=phone,
                parent_password=plain_password,
            ))

    return ImportResultOut(
        created=created,
        skipped=skipped,
        errors=errors,
        credentials=credentials,
    )


# ── Delete student ────────────────────────────────────────


@router.patch("/students/{student_id}", response_model=StudentOut)
async def update_student(
    student_id: str, body: StudentPatch, user: TeacherDep, session: SessionDep
) -> StudentOut:
    """Edit a student in this teacher's home-grade classroom — name, roll #, classroom, parent."""
    tid = assert_tenant(user)
    student = await _get_student_in_my_home_class(student_id, user.id, tid, session)

    if body.full_name is not None:
        student.full_name = body.full_name

    if body.handle is not None and body.handle != student.handle:
        dup = await session.scalar(
            select(Student).where(
                Student.handle == body.handle,
                Student.tenant_id == tid,
                Student.id != student.id,
            )
        )
        if dup:
            raise HTTPException(409, "Roll number already in use in this school")
        student.handle = body.handle

    if body.classroom_id is not None:
        try:
            cid = uuid.UUID(body.classroom_id)
        except ValueError:
            raise HTTPException(400, "invalid classroom_id")
        home_ids = await _teacher_home_classroom_ids(session, tid, user.id)
        if cid not in home_ids:
            raise HTTPException(404, "classroom not found in your home grade")
        cls = await session.get(Classroom, cid)
        if not cls:
            raise HTTPException(404, "classroom not found in your home grade")
        student.classroom_id = cls.id
        student.grade = cls.grade
        student.section = cls.section

    new_phone = body.parent_phone.strip() if body.parent_phone else None
    new_pw = body.parent_password.strip() if body.parent_password else None
    parent = await _linked_parent(session, student.id)

    if new_phone:
        clash = await session.scalar(
            select(User).where(User.phone == new_phone, User.role != UserRole.parent)
        )
        if clash:
            raise HTTPException(409, "Phone number is registered to a non-parent account")

    if parent:
        if body.parent_name is not None:
            parent.full_name = body.parent_name
        if new_pw:
            parent.password_hash = hash_password(new_pw)
        if new_phone and new_phone != parent.phone:
            other = await session.scalar(
                select(User).where(
                    User.phone == new_phone,
                    User.role == UserRole.parent,
                    User.id != parent.id,
                )
            )
            if other:
                from sqlalchemy import delete as sql_delete
                await session.execute(
                    sql_delete(ParentStudent).where(
                        ParentStudent.parent_id == parent.id,
                        ParentStudent.student_id == student.id,
                    )
                )
                await _link_parent_student(other.id, student.id, session)
                if body.parent_name is not None:
                    other.full_name = body.parent_name
                if new_pw:
                    other.password_hash = hash_password(new_pw)
            else:
                from backend.db.repositories.users import normalize_phone
                parent.phone = new_phone
                parent.phone_digits = normalize_phone(new_phone)
            student.parent_phone = new_phone
    elif new_phone:
        new_parent = await _find_or_create_parent(
            phone=new_phone, password=new_pw or new_phone,
            full_name=body.parent_name, tenant_id=tid, session=session,
        )
        if new_pw:
            new_parent.password_hash = hash_password(new_pw)
        await _link_parent_student(new_parent.id, student.id, session)
        student.parent_phone = new_phone

    await session.flush()
    await session.refresh(student)
    return await _student_out(student, session)


@router.delete("/students/{student_id}", status_code=200)
async def delete_student(
    student_id: str, user: TeacherDep, session: SessionDep
) -> dict:
    from sqlalchemy import delete as sql_delete
    from sqlalchemy.exc import IntegrityError
    tid = assert_tenant(user)
    student = await _get_student_in_my_home_class(student_id, user.id, tid, session)

    parent_ids = set((await session.execute(
        select(ParentStudent.parent_id).where(ParentStudent.student_id == student.id)
    )).scalars().all())
    student_user_id = student.user_id

    try:
        await session.execute(sql_delete(Student).where(Student.id == student.id))
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "Cannot delete: student has linked assessment data.")

    # Also remove the dedicated student User row so its handle is freed up
    # for a future re-onboard. Wrapped in a savepoint so a FK conflict
    # (e.g. lingering conversations) leaves the row in place without
    # undoing the student delete that already succeeded.
    if student_user_id is not None:
        try:
            async with session.begin_nested():
                await session.execute(sql_delete(User).where(User.id == student_user_id))
        except IntegrityError:
            pass

    await _delete_orphan_parents(session, parent_ids)
    return {"deleted": True}


# ── Parent account ───────────────────────────────────────


@router.post(
    "/students/{student_id}/parent",
    response_model=ParentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_parent(
    student_id: str, body: ParentIn, user: TeacherDep, session: SessionDep
) -> ParentOut:
    """Create a parent account and link it to the student."""
    tid = assert_tenant(user)
    student = await _get_student_in_my_class(student_id, user.id, tid, session)

    # Reject if phone belongs to a non-parent account
    if await session.scalar(select(User).where(User.phone == body.phone, User.role != UserRole.parent)):
        raise HTTPException(409, "Phone number is registered to a non-parent account")

    parent = await _find_or_create_parent(
        phone=body.phone, password=body.password,
        full_name=body.full_name, tenant_id=tid, session=session,
    )
    await _link_parent_student(parent.id, student.id, session)
    student.parent_phone = body.phone
    await session.flush()
    await session.refresh(parent)
    return ParentOut(id=str(parent.id), phone=parent.phone or "", full_name=parent.full_name)


# ── Attendance ────────────────────────────────────────────


@router.post("/attendance", response_model=AttendanceOut)
async def mark_attendance(
    body: AttendanceIn, user: TeacherDep, session: SessionDep
) -> AttendanceOut:
    tid = assert_tenant(user)
    await _get_student_in_my_class(body.student_id, user.id, tid, session)
    repo = AttendanceRepository(session)
    n = await repo.bulk_upsert([{
        "student_id": uuid.UUID(body.student_id),
        "date": body.date,
        "status": body.status,
    }])
    return AttendanceOut(saved=n)


@router.post("/attendance/bulk", response_model=AttendanceOut)
async def mark_attendance_bulk(
    body: BulkAttendanceIn, user: TeacherDep, session: SessionDep
) -> AttendanceOut:
    """Mark attendance for a whole classroom in one call.
    Allowed for any classroom the teacher is assigned to (home grade or subject)."""
    tid = assert_tenant(user)
    my_cls_ids = await _teacher_classroom_ids(session, tid, user.id)

    rows = []
    skipped_ids: list[str] = []
    for rec in body.records:
        sid = rec.get("student_id", "")
        try:
            student_uuid = uuid.UUID(sid)
        except ValueError:
            skipped_ids.append(str(sid))
            continue
        result = await session.execute(
            select(Student).where(
                Student.id == student_uuid,
                Student.tenant_id == tid,
                Student.classroom_id.in_(my_cls_ids),
            )
        )
        if not result.scalar_one_or_none():
            skipped_ids.append(sid)
            continue
        raw_status = rec.get("status", "present")
        try:
            att_status = AttendanceStatus(raw_status)
        except ValueError:
            att_status = AttendanceStatus.present
        rows.append({"student_id": student_uuid, "date": body.date, "status": att_status})

    if not rows:
        return AttendanceOut(saved=0, skipped=len(skipped_ids), skipped_student_ids=skipped_ids)
    repo = AttendanceRepository(session)
    n = await repo.bulk_upsert(rows)

    # Alert parents whose children fall below the attendance threshold (rolling 30d)
    from backend.api._notify import maybe_alert_low_attendance
    await maybe_alert_low_attendance(session, {r["student_id"] for r in rows})

    return AttendanceOut(saved=n, skipped=len(skipped_ids), skipped_student_ids=skipped_ids)


class StudentAttendanceRow(BaseModel):
    student_id: str
    student_name: str
    handle: str
    present: int
    absent: int
    late: int
    unmarked: int
    attendance_pct: float | None   # null if no days marked


class DayCountsRow(BaseModel):
    date: date
    present: int
    absent: int
    late: int
    unmarked: int


class ClassAttendanceHistoryOut(BaseModel):
    classroom_id: str
    classroom_name: str
    from_date: date
    to_date: date
    students: list[StudentAttendanceRow]
    days: list[DayCountsRow]    # per-day classroom rollup
    working_days: int


@router.get(
    "/attendance/history",
    response_model=ClassAttendanceHistoryOut,
)
async def class_attendance_history(
    user: TeacherDep,
    session: SessionDep,
    classroom_id: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> ClassAttendanceHistoryOut:
    """Attendance breakdown for a classroom over a date range.
    Two views: per-student rollup (counts + percentage) and per-day rollup
    (how many present/absent/late on each working day)."""
    from backend.db.models import Attendance as AttendanceModel
    tid = assert_tenant(user)
    cls = await _get_accessible_classroom(classroom_id, user.id, tid, session)

    today = date.today()
    end = to_date or today
    start = from_date or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(400, "from_date must be on or before to_date")
    if (end - start).days > 366:
        raise HTTPException(400, "date range cannot exceed one year")

    # Working days = Mon-Sat (Sunday off by default)
    working_dates: list[date] = []
    d = start
    while d <= end:
        if d.isoweekday() != 7:
            working_dates.append(d)
        d += timedelta(days=1)

    s_result = await session.execute(
        select(Student)
        .where(Student.classroom_id == cls.id, Student.tenant_id == tid)
        .order_by(Student.full_name)
    )
    students = s_result.scalars().all()

    if not students:
        return ClassAttendanceHistoryOut(
            classroom_id=str(cls.id),
            classroom_name=cls.name,
            from_date=start, to_date=end,
            students=[], days=[],
            working_days=len(working_dates),
        )

    student_ids = [s.id for s in students]
    a_result = await session.execute(
        select(AttendanceModel)
        .where(
            AttendanceModel.student_id.in_(student_ids),
            AttendanceModel.date >= start,
            AttendanceModel.date <= end,
        )
    )
    by_student: dict[uuid.UUID, dict[date, str]] = {sid: {} for sid in student_ids}
    by_day: dict[date, dict[str, int]] = {
        d: {"present": 0, "absent": 0, "late": 0, "unmarked": 0}
        for d in working_dates
    }
    for a in a_result.scalars().all():
        by_student[a.student_id][a.date] = a.status.value
        if a.date in by_day:
            by_day[a.date][a.status.value] += 1

    student_rows: list[StudentAttendanceRow] = []
    for s in students:
        recs = by_student.get(s.id, {})
        p = a = l = u = 0
        for d in working_dates:
            st = recs.get(d, "unmarked")
            if st == "present": p += 1
            elif st == "absent": a += 1
            elif st == "late":   l += 1
            else: u += 1
        marked = p + a + l
        pct = round((p + l) / marked * 100, 1) if marked else None
        student_rows.append(StudentAttendanceRow(
            student_id=str(s.id), student_name=s.full_name, handle=s.handle,
            present=p, absent=a, late=l, unmarked=u, attendance_pct=pct,
        ))

    # Unmarked count per day = total students - marked
    day_rows: list[DayCountsRow] = []
    total_students = len(students)
    for d in working_dates:
        d_counts = by_day[d]
        marked_today = d_counts["present"] + d_counts["absent"] + d_counts["late"]
        d_counts["unmarked"] = max(0, total_students - marked_today)
        day_rows.append(DayCountsRow(
            date=d,
            present=d_counts["present"],
            absent=d_counts["absent"],
            late=d_counts["late"],
            unmarked=d_counts["unmarked"],
        ))

    return ClassAttendanceHistoryOut(
        classroom_id=str(cls.id),
        classroom_name=cls.name,
        from_date=start, to_date=end,
        students=student_rows, days=day_rows,
        working_days=len(working_dates),
    )


@router.get("/attendance/history.csv")
async def class_attendance_history_csv(
    user: TeacherDep,
    session: SessionDep,
    classroom_id: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> Response:
    """Same data as /attendance/history, served as a CSV download.
    One row per student, columns: roll #, name, P, A, L, unmarked, %.
    """
    import csv as _csv
    import io as _io
    data = await class_attendance_history(
        user=user, session=session,
        classroom_id=classroom_id,
        from_date=from_date, to_date=to_date,
    )
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "Roll #", "Student", "Present", "Absent", "Late", "Unmarked", "Attendance %",
    ])
    for s in data.students:
        w.writerow([
            s.handle, s.student_name, s.present, s.absent, s.late, s.unmarked,
            "" if s.attendance_pct is None else f"{s.attendance_pct}",
        ])
    csv_text = buf.getvalue()
    filename = f"attendance_{data.classroom_name}_{data.from_date}_{data.to_date}.csv".replace(" ", "_")
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/attendance", response_model=AttendanceDayOut)
async def get_attendance(
    user: TeacherDep,
    session: SessionDep,
    classroom_id: str,
    date: date,
) -> AttendanceDayOut:
    """Return all students in a classroom with their attendance status for a given date.
    Allowed for any classroom the teacher is assigned to (home grade or subject)."""
    from backend.db.models import Attendance as AttendanceModel
    tid = assert_tenant(user)
    cls = await _get_accessible_classroom(classroom_id, user.id, tid, session)

    result = await session.execute(
        select(Student)
        .where(Student.classroom_id == cls.id, Student.tenant_id == tid)
        .order_by(Student.full_name)
    )
    students = result.scalars().all()

    att_result = await session.execute(
        select(AttendanceModel).where(
            AttendanceModel.student_id.in_([s.id for s in students]),
            AttendanceModel.date == date,
        )
    )
    att_map = {str(a.student_id): a.status.value for a in att_result.scalars().all()}

    return AttendanceDayOut(
        date=date,
        records=[
            AttendanceRecordOut(
                student_id=str(s.id),
                student_name=s.full_name,
                status=att_map.get(str(s.id)),
            )
            for s in students
        ],
    )


# ── Assignments ──────────────────────────────────────────


class QuestionIn(BaseModel):
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str   # 'a' | 'b' | 'c' | 'd'


class QuestionOut(BaseModel):
    id: str
    order_num: int
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str


class AssignmentIn(BaseModel):
    classroom_id: str
    subject: str
    title: str | None = None
    questions: list[QuestionIn]   # at least 1 required
    due_date: date | None = None
    publish: bool = False


class AssignmentPatch(BaseModel):
    subject: str | None = None
    title: str | None = None
    questions: list[QuestionIn] | None = None   # if provided, replaces all questions
    due_date: date | None = None


class AssignmentOut(BaseModel):
    id: str
    classroom_id: str
    classroom_name: str | None
    subject: str
    title: str | None
    questions: list[QuestionOut]
    status: str
    due_date: date | None
    created_at: datetime


async def _assignment_out(a: Assignment, cls_name: str | None, session: object) -> AssignmentOut:
    q_result = await session.execute(  # type: ignore[union-attr]
        select(AssignmentQuestion)
        .where(AssignmentQuestion.assignment_id == a.id)
        .order_by(AssignmentQuestion.order_num)
    )
    questions = [
        QuestionOut(
            id=str(q.id), order_num=q.order_num,
            question_text=q.question_text,
            option_a=q.option_a, option_b=q.option_b,
            option_c=q.option_c, option_d=q.option_d,
            correct_option=q.correct_option,
        )
        for q in q_result.scalars().all()
    ]
    return AssignmentOut(
        id=str(a.id), classroom_id=str(a.classroom_id), classroom_name=cls_name,
        subject=a.subject, title=a.title,
        questions=questions,
        status=a.status.value, due_date=a.due_date, created_at=a.created_at,
    )


@router.get("/assignments", response_model=list[AssignmentOut])
async def list_assignments(
    user: TeacherDep,
    session: SessionDep,
    classroom_id: str | None = None,
    status: str | None = None,
) -> list[AssignmentOut]:
    tid = assert_tenant(user)
    cr = await session.execute(
        select(Classroom).where(Classroom.tenant_id == tid, Classroom.teacher_id == user.id)
    )
    home_cls_ids = [c.id for c in cr.scalars().all()]

    sa_res = await session.execute(
        select(TeacherSubjectAssignment).where(TeacherSubjectAssignment.teacher_id == user.id)
    )
    subject_cls_ids = [sa_rec.classroom_id for sa_rec in sa_res.scalars().all()]

    all_cls_ids = list({*home_cls_ids, *subject_cls_ids})
    if not all_cls_ids:
        return []

    stmt = select(Assignment).where(
        Assignment.teacher_id == user.id,
        Assignment.tenant_id == tid,
    )
    if classroom_id:
        try:
            cid = uuid.UUID(classroom_id)
        except ValueError:
            raise HTTPException(400, "invalid classroom_id")
        stmt = stmt.where(Assignment.classroom_id == cid)
    if status in ("draft", "published"):
        stmt = stmt.where(Assignment.status == AssignmentStatus(status))

    result = await session.execute(stmt.order_by(Assignment.created_at.desc()))
    assignments = result.scalars().all()

    out = []
    for a in assignments:
        cls = await session.get(Classroom, a.classroom_id)
        out.append(await _assignment_out(a, cls.name if cls else None, session))
    return out


@router.post("/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    body: AssignmentIn, user: TeacherDep, session: SessionDep
) -> AssignmentOut:
    tid = assert_tenant(user)
    cls = await _get_accessible_classroom(body.classroom_id, user.id, tid, session)

    if not body.questions:
        raise HTTPException(400, "at least one question is required")
    for i, q in enumerate(body.questions, start=1):
        if q.correct_option not in ("a", "b", "c", "d"):
            raise HTTPException(400, f"question {i}: correct_option must be a, b, c, or d")

    # Guard against accidental duplicates — same teacher, classroom, subject
    # and (case-insensitive) title.
    effective_title = (body.title or body.subject).strip().lower()
    dup_rows = await session.execute(
        select(Assignment).where(
            Assignment.teacher_id == user.id,
            Assignment.classroom_id == cls.id,
            Assignment.subject == body.subject,
        )
    )
    for existing in dup_rows.scalars().all():
        existing_title = (existing.title or existing.subject).strip().lower()
        if existing_title == effective_title:
            raise HTTPException(
                409,
                f"You already have an assignment titled '{body.title or body.subject}' "
                f"for this classroom and subject.",
            )

    a = Assignment(
        id=uuid.uuid4(), tenant_id=tid, teacher_id=user.id,
        classroom_id=cls.id, subject=body.subject, title=body.title,
        status=AssignmentStatus.published if body.publish else AssignmentStatus.draft,
        due_date=body.due_date,
    )
    session.add(a)
    await session.flush()

    for i, q in enumerate(body.questions, start=1):
        session.add(AssignmentQuestion(
            id=uuid.uuid4(), assignment_id=a.id, order_num=i,
            question_text=q.question_text,
            option_a=q.option_a, option_b=q.option_b,
            option_c=q.option_c, option_d=q.option_d,
            correct_option=q.correct_option,
        ))
    await session.flush()

    if body.publish:
        from backend.api._notify import notify_assignment_published
        await notify_assignment_published(
            session, cls.id, body.title or body.subject, a.id
        )

    return await _assignment_out(a, cls.name, session)


@router.patch("/assignments/{assignment_id}", response_model=AssignmentOut)
async def edit_assignment(
    assignment_id: str, body: AssignmentPatch, user: TeacherDep, session: SessionDep
) -> AssignmentOut:
    from sqlalchemy import delete as sql_delete
    a, cls = await _get_my_assignment(assignment_id, user, session)
    if body.subject is not None:
        a.subject = body.subject
    if body.title is not None:
        a.title = body.title
    if "due_date" in body.model_fields_set:
        a.due_date = body.due_date
    if body.questions is not None:
        if not body.questions:
            raise HTTPException(400, "at least one question is required")
        for i, q in enumerate(body.questions, start=1):
            if q.correct_option not in ("a", "b", "c", "d"):
                raise HTTPException(400, f"question {i}: correct_option must be a, b, c, or d")
        await session.execute(
            sql_delete(AssignmentQuestion).where(AssignmentQuestion.assignment_id == a.id)
        )
        for i, q in enumerate(body.questions, start=1):
            session.add(AssignmentQuestion(
                id=uuid.uuid4(), assignment_id=a.id, order_num=i,
                question_text=q.question_text,
                option_a=q.option_a, option_b=q.option_b,
                option_c=q.option_c, option_d=q.option_d,
                correct_option=q.correct_option,
            ))
    await session.flush()
    return await _assignment_out(a, cls.name if cls else None, session)


@router.patch("/assignments/{assignment_id}/publish", response_model=AssignmentOut)
async def publish_assignment(
    assignment_id: str, user: TeacherDep, session: SessionDep
) -> AssignmentOut:
    from backend.api._notify import notify_assignment_published
    a, cls = await _get_my_assignment(assignment_id, user, session)
    was_draft = a.status is AssignmentStatus.draft
    a.status = AssignmentStatus.published
    await session.flush()
    if was_draft:
        await notify_assignment_published(
            session, a.classroom_id, a.title or a.subject, a.id
        )
    return await _assignment_out(a, cls.name if cls else None, session)


@router.patch("/assignments/{assignment_id}/unpublish", response_model=AssignmentOut)
async def unpublish_assignment(
    assignment_id: str, user: TeacherDep, session: SessionDep
) -> AssignmentOut:
    a, cls = await _get_my_assignment(assignment_id, user, session)
    a.status = AssignmentStatus.draft
    await session.flush()
    return await _assignment_out(a, cls.name if cls else None, session)


@router.delete("/assignments/{assignment_id}", status_code=200)
async def delete_assignment(
    assignment_id: str, user: TeacherDep, session: SessionDep
) -> dict:
    from sqlalchemy import delete as sql_delete
    a, _ = await _get_my_assignment(assignment_id, user, session)
    await session.execute(sql_delete(Assignment).where(Assignment.id == a.id))
    await session.flush()
    return {"deleted": True}


class SubmissionRow(BaseModel):
    student_id: str
    student_name: str
    handle: str
    submitted: bool
    score: int | None = None
    total: int | None = None
    score_pct: float | None = None
    submitted_at: datetime | None = None


@router.get("/assignments/{assignment_id}/submissions", response_model=list[SubmissionRow])
async def assignment_submissions(
    assignment_id: str, user: TeacherDep, session: SessionDep
) -> list[SubmissionRow]:
    """Per-student breakdown for one assignment: who finished, who didn't, scores."""
    tid = assert_tenant(user)
    a, _ = await _get_my_assignment(assignment_id, user, session)

    s_result = await session.execute(
        select(Student)
        .where(Student.classroom_id == a.classroom_id, Student.tenant_id == tid)
        .order_by(Student.full_name)
    )
    students = s_result.scalars().all()

    r_result = await session.execute(
        select(AssignmentResponse).where(AssignmentResponse.assignment_id == a.id)
    )
    resp_map = {r.student_id: r for r in r_result.scalars().all()}

    rows: list[SubmissionRow] = []
    for s in students:
        r = resp_map.get(s.id)
        if r:
            pct = (r.score / r.total_questions * 100) if r.total_questions else 0.0
            rows.append(SubmissionRow(
                student_id=str(s.id), student_name=s.full_name, handle=s.handle,
                submitted=True, score=r.score, total=r.total_questions,
                score_pct=round(pct, 1), submitted_at=r.submitted_at,
            ))
        else:
            rows.append(SubmissionRow(
                student_id=str(s.id), student_name=s.full_name, handle=s.handle,
                submitted=False,
            ))
    return rows


@router.get("/assignments/{assignment_id}/stats")
async def assignment_stats(
    assignment_id: str, user: TeacherDep, session: SessionDep
) -> dict:
    from sqlalchemy import func
    tid = assert_tenant(user)
    a, _ = await _get_my_assignment(assignment_id, user, session)

    total_students = await session.scalar(
        select(func.count()).where(
            Student.classroom_id == a.classroom_id,
            Student.tenant_id == tid,
        )
    ) or 0

    result = await session.execute(
        select(AssignmentResponse).where(AssignmentResponse.assignment_id == a.id)
    )
    responses = result.scalars().all()

    attempted       = len(responses)
    all_correct     = sum(1 for r in responses if r.total_questions > 0 and r.score == r.total_questions)
    all_wrong       = sum(1 for r in responses if r.total_questions > 0 and r.score == 0)
    partial         = attempted - all_correct - all_wrong

    avg_score_pct   = 0.0
    if responses:
        valid = [r for r in responses if r.total_questions > 0]
        if valid:
            avg_score_pct = sum(r.score / r.total_questions * 100 for r in valid) / len(valid)

    attempted_pct    = (attempted    / total_students * 100) if total_students else 0.0
    all_correct_pct  = (all_correct  / attempted      * 100) if attempted      else 0.0
    all_wrong_pct    = (all_wrong    / attempted      * 100) if attempted      else 0.0
    partial_pct      = (partial      / attempted      * 100) if attempted      else 0.0

    return {
        "total_students":  total_students,
        "attempted":       attempted,
        "attempted_pct":   round(attempted_pct,   1),
        "all_correct":     all_correct,
        "all_correct_pct": round(all_correct_pct, 1),
        "partial":         partial,
        "partial_pct":     round(partial_pct,     1),
        "all_wrong":       all_wrong,
        "all_wrong_pct":   round(all_wrong_pct,   1),
        "avg_score_pct":   round(avg_score_pct,   1),
        "topic_understood": (avg_score_pct >= 70) if attempted else None,
    }


async def _get_my_assignment(
    assignment_id: str, user: User, session: object
) -> tuple[Assignment, Classroom]:
    try:
        aid = uuid.UUID(assignment_id)
    except ValueError:
        raise HTTPException(400, "invalid assignment_id")
    a = await session.scalar(  # type: ignore[union-attr]
        select(Assignment).where(Assignment.id == aid, Assignment.teacher_id == user.id)
    )
    if not a:
        raise HTTPException(404, "assignment not found or not yours")
    cls = await session.get(Classroom, a.classroom_id)  # type: ignore[union-attr]
    return a, cls


# ── Shared parent helpers ─────────────────────────────────


async def _find_or_create_parent(
    *,
    phone: str,
    password: str,
    full_name: str | None,
    tenant_id: uuid.UUID,
    session: object,
) -> User:
    """Return the parent for this phone (creating one if absent).

    Sibling onboarding: when the phone already belongs to a parent in the
    same tenant, reuse that account — the provided `password` and
    `full_name` are *not* applied to the existing user (we don't silently
    change a parent's credentials from a child's onboarding form).

    Cross-tenant safety: `users.phone` is globally unique, so a phone that
    already exists in another school cannot back a parent here. Surface a
    409 instead of silently linking a child to a parent from another tenant.
    """
    existing = await session.scalar(  # type: ignore[union-attr]
        select(User).where(User.phone == phone, User.role == UserRole.parent)
    )
    if existing:
        if existing.tenant_id != tenant_id:
            raise HTTPException(
                409,
                "This phone number is already registered to a parent at "
                "another school. Use a different phone or have the family "
                "transfer between schools first.",
            )
        return existing
    from backend.db.repositories.users import normalize_phone
    parent = User(
        id=uuid.uuid4(), tenant_id=tenant_id, phone=phone,
        phone_digits=normalize_phone(phone),
        password_hash=hash_password(password),
        role=UserRole.parent, full_name=full_name,
    )
    session.add(parent)  # type: ignore[union-attr]
    await session.flush()  # type: ignore[union-attr]
    return parent


async def _link_parent_student(
    parent_id: uuid.UUID,
    student_id: uuid.UUID,
    session: object,
) -> None:
    """Add parent-student link if it doesn't already exist."""
    exists = await session.scalar(  # type: ignore[union-attr]
        select(ParentStudent).where(
            ParentStudent.parent_id == parent_id,
            ParentStudent.student_id == student_id,
        )
    )
    if not exists:
        session.add(ParentStudent(id=uuid.uuid4(), parent_id=parent_id, student_id=student_id))  # type: ignore[union-attr]
        await session.flush()  # type: ignore[union-attr]


# ── Helpers ───────────────────────────────────────────────


async def _linked_parent(session, student_id: uuid.UUID) -> User | None:
    """First parent User linked to a student via ParentStudent, if any."""
    link = await session.scalar(
        select(ParentStudent).where(ParentStudent.student_id == student_id)
    )
    if not link:
        return None
    return await session.get(User, link.parent_id)


async def _delete_orphan_parents(session, parent_ids) -> None:
    """Remove parent accounts with no remaining linked students.

    Parents are shared across siblings, so only delete once the last child is
    gone. If other records still reference the parent, leave the account.
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


async def _student_out(s: Student, session: object) -> StudentOut:
    cls_name = None
    if s.classroom_id:
        cls = await session.get(Classroom, s.classroom_id)  # type: ignore[union-attr]
        cls_name = cls.name if cls else None
    parent = await _linked_parent(session, s.id)
    return StudentOut(
        id=str(s.id), handle=s.handle, full_name=s.full_name,
        grade=s.grade, section=s.section,
        classroom_id=str(s.classroom_id) if s.classroom_id else None,
        classroom_name=cls_name, parent_phone=s.parent_phone,
        parent_name=parent.full_name if parent else None,
    )


async def _get_my_classroom(
    classroom_id: str, teacher_id: uuid.UUID, tenant_id: uuid.UUID, session: object
) -> Classroom:
    try:
        cid = uuid.UUID(classroom_id)
    except ValueError:
        raise HTTPException(400, "invalid classroom_id")
    result = await session.execute(  # type: ignore[union-attr]
        select(Classroom).where(
            Classroom.id == cid,
            Classroom.tenant_id == tenant_id,
            Classroom.teacher_id == teacher_id,
        )
    )
    cls = result.scalar_one_or_none()
    if not cls:
        raise HTTPException(
            403,
            "You must be the home (class) teacher of this classroom to onboard "
            "or modify students here. Subject-only assignments don't grant "
            "onboarding rights — ask the school admin or the home teacher.",
        )
    return cls


async def _get_accessible_classroom(
    classroom_id: str, teacher_id: uuid.UUID, tenant_id: uuid.UUID, session: object
) -> Classroom:
    """Return classroom if teacher is home teacher OR has a subject assignment for it."""
    try:
        cid = uuid.UUID(classroom_id)
    except ValueError:
        raise HTTPException(400, "invalid classroom_id")
    cls = await session.scalar(  # type: ignore[union-attr]
        select(Classroom).where(
            Classroom.id == cid,
            Classroom.tenant_id == tenant_id,
        )
    )
    if not cls:
        raise HTTPException(404, "classroom not found")
    if cls.teacher_id == teacher_id:
        return cls
    sa_rec = await session.scalar(  # type: ignore[union-attr]
        select(TeacherSubjectAssignment).where(
            TeacherSubjectAssignment.teacher_id == teacher_id,
            TeacherSubjectAssignment.classroom_id == cid,
        )
    )
    if sa_rec:
        return cls
    raise HTTPException(403, "classroom not accessible to you")


async def _get_student_in_my_class(
    student_id: str, teacher_id: uuid.UUID, tenant_id: uuid.UUID, session: object
) -> Student:
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise HTTPException(400, "invalid student_id")

    my_cls_ids = await _teacher_classroom_ids(session, tenant_id, teacher_id)

    result = await session.execute(  # type: ignore[union-attr]
        select(Student).where(
            Student.id == sid,
            Student.tenant_id == tenant_id,
            Student.classroom_id.in_(my_cls_ids),
        )
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(403, "student not found in your classrooms")
    return student


async def _get_student_in_my_home_class(
    student_id: str, teacher_id: uuid.UUID, tenant_id: uuid.UUID, session: object
) -> Student:
    """Edit/delete is allowed only for students in the teacher's home-grade class.
    Subject-only classrooms are view-only."""
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise HTTPException(400, "invalid student_id")

    home_ids = await _teacher_home_classroom_ids(session, tenant_id, teacher_id)
    if not home_ids:
        raise HTTPException(403, "edit/delete only allowed for your home-grade students")

    result = await session.execute(  # type: ignore[union-attr]
        select(Student).where(
            Student.id == sid,
            Student.tenant_id == tenant_id,
            Student.classroom_id.in_(home_ids),
        )
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(403, "edit/delete only allowed for your home-grade students")
    return student
