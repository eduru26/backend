"""Announcements — posts from school admin / teachers to targeted audiences."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from backend.api.deps import CurrentUser, SessionDep, assert_tenant
from backend.db.models import (
    Announcement, AnnouncementAudience, Classroom, Notification, NotificationKind,
    ParentStudent, Student, TeacherSubjectAssignment, User, UserRole,
)

router = APIRouter(prefix="/announcements", tags=["announcements"])


# ── Schemas ──────────────────────────────────────────────


class AnnouncementIn(BaseModel):
    title: str
    body: str
    audience: AnnouncementAudience
    classroom_id: str | None = None
    pinned: bool = False


class AnnouncementPatch(BaseModel):
    title: str | None = None
    body: str | None = None
    pinned: bool | None = None


class AnnouncementOut(BaseModel):
    id: str
    title: str
    body: str
    audience: str
    classroom_id: str | None
    classroom_name: str | None
    author_id: str
    author_name: str | None
    pinned: bool
    created_at: datetime


# ── Helpers ──────────────────────────────────────────────


def _can_post(user: User) -> bool:
    return user.role in {UserRole.school_admin, UserRole.teacher}


async def _audiences_for(user: User, session) -> list[AnnouncementAudience]:
    """Which announcement audiences a user is entitled to see."""
    role = user.role
    if role is UserRole.school_admin:
        return list(AnnouncementAudience)
    if role is UserRole.teacher:
        return [
            AnnouncementAudience.school,
            AnnouncementAudience.teachers,
            AnnouncementAudience.classroom,
        ]
    if role is UserRole.parent:
        return [
            AnnouncementAudience.school,
            AnnouncementAudience.parents,
            AnnouncementAudience.classroom,
        ]
    if role is UserRole.student:
        return [
            AnnouncementAudience.school,
            AnnouncementAudience.students,
            AnnouncementAudience.classroom,
        ]
    return [AnnouncementAudience.school]


async def _student_classroom_ids(user: User, session) -> list[uuid.UUID]:
    """Classroom ids relevant to a parent (their kids) or student (theirs)."""
    if user.role is UserRole.student:
        sid = getattr(user, "_student_id", None)
        if not sid:
            return []
        stu = await session.get(Student, sid)
        return [stu.classroom_id] if stu and stu.classroom_id else []
    if user.role is UserRole.parent:
        rows = await session.execute(
            select(Student.classroom_id)
            .join(ParentStudent, ParentStudent.student_id == Student.id)
            .where(ParentStudent.parent_id == user.id)
        )
        return [r for r in rows.scalars().all() if r is not None]
    return []


def _to_out(a: Announcement) -> AnnouncementOut:
    return AnnouncementOut(
        id=str(a.id),
        title=a.title,
        body=a.body,
        audience=a.audience.value,
        classroom_id=str(a.classroom_id) if a.classroom_id else None,
        classroom_name=a.classroom.name if a.classroom else None,
        author_id=str(a.author_id),
        author_name=a.author.full_name if a.author else None,
        pinned=a.pinned,
        created_at=a.created_at,
    )


# ── Endpoints ────────────────────────────────────────────


@router.get("", response_model=list[AnnouncementOut])
async def list_announcements(
    user: CurrentUser,
    session: SessionDep,
    limit: int = 50,
) -> list[AnnouncementOut]:
    tenant_id = assert_tenant(user)
    audiences = await _audiences_for(user, session)
    classroom_ids = await _student_classroom_ids(user, session)

    stmt = (
        select(Announcement)
        .options(selectinload(Announcement.author), selectinload(Announcement.classroom))
        .where(
            Announcement.tenant_id == tenant_id,
            Announcement.audience.in_(audiences),
        )
    )

    # Classroom-targeted announcements only visible to those linked to that classroom
    # (school_admin sees everything, teachers see their own classrooms — for now, all).
    if user.role in {UserRole.parent, UserRole.student}:
        if classroom_ids:
            stmt = stmt.where(
                or_(
                    Announcement.audience != AnnouncementAudience.classroom,
                    Announcement.classroom_id.in_(classroom_ids),
                )
            )
        else:
            stmt = stmt.where(Announcement.audience != AnnouncementAudience.classroom)

    stmt = stmt.order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(a) for a in rows]


@router.post("", response_model=AnnouncementOut, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    payload: AnnouncementIn,
    user: CurrentUser,
    session: SessionDep,
) -> AnnouncementOut:
    tenant_id = assert_tenant(user)
    if not _can_post(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only school_admin or teacher can post")

    # Teachers can only post to a classroom they actually teach.
    if user.role is UserRole.teacher and payload.audience is not AnnouncementAudience.classroom:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Teachers can only post classroom announcements",
        )

    classroom_uuid: uuid.UUID | None = None
    if payload.audience is AnnouncementAudience.classroom:
        if not payload.classroom_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "classroom_id required for classroom audience")
        try:
            classroom_uuid = uuid.UUID(payload.classroom_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid classroom_id")
        classroom = await session.get(Classroom, classroom_uuid)
        if not classroom or classroom.tenant_id != tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "classroom not found")

        if user.role is UserRole.teacher:
            is_home = classroom.teacher_id == user.id
            is_subject = await session.scalar(
                select(TeacherSubjectAssignment.id).where(
                    TeacherSubjectAssignment.teacher_id == user.id,
                    TeacherSubjectAssignment.classroom_id == classroom_uuid,
                )
            )
            if not is_home and not is_subject:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "You can only post to classrooms you teach",
                )

    a = Announcement(
        tenant_id=tenant_id,
        author_id=user.id,
        audience=payload.audience,
        classroom_id=classroom_uuid,
        title=payload.title,
        body=payload.body,
        pinned=payload.pinned,
    )
    session.add(a)
    await session.flush()

    # Fan-out notifications to recipients
    await _fanout_notifications(a, session)

    await session.refresh(a, attribute_names=["author", "classroom"])
    return _to_out(a)


@router.get("/{announcement_id}", response_model=AnnouncementOut)
async def get_announcement(
    announcement_id: str,
    user: CurrentUser,
    session: SessionDep,
) -> AnnouncementOut:
    """Fetch a single announcement — same visibility rules as the list endpoint."""
    tenant_id = assert_tenant(user)
    try:
        aid = uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid announcement id")
    a = await session.scalar(
        select(Announcement)
        .options(selectinload(Announcement.author), selectinload(Announcement.classroom))
        .where(Announcement.id == aid)
    )
    if not a or a.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    audiences = await _audiences_for(user, session)
    if a.audience not in audiences:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not visible to you")

    if user.role in {UserRole.parent, UserRole.student} and a.audience is AnnouncementAudience.classroom:
        classroom_ids = await _student_classroom_ids(user, session)
        if a.classroom_id not in classroom_ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not visible to you")

    return _to_out(a)


@router.patch("/{announcement_id}", response_model=AnnouncementOut)
async def update_announcement(
    announcement_id: str,
    payload: AnnouncementPatch,
    user: CurrentUser,
    session: SessionDep,
) -> AnnouncementOut:
    """Edit title/body/pinned. Author (or school_admin) only."""
    tenant_id = assert_tenant(user)
    try:
        aid = uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid announcement id")
    a = await session.scalar(
        select(Announcement)
        .options(selectinload(Announcement.author), selectinload(Announcement.classroom))
        .where(Announcement.id == aid)
    )
    if not a or a.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if user.role is not UserRole.school_admin and a.author_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot edit others' announcements")

    if payload.title is not None:
        a.title = payload.title
    if payload.body is not None:
        a.body = payload.body
    if payload.pinned is not None:
        a.pinned = payload.pinned
    await session.flush()
    return _to_out(a)


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(
    announcement_id: str,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    from sqlalchemy import delete as sql_delete
    tenant_id = assert_tenant(user)
    try:
        aid = uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid announcement id")
    a = await session.get(Announcement, aid)
    if not a or a.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if user.role is not UserRole.school_admin and a.author_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot delete others' announcements")

    # Tidy up the fan-out notifications so users don't see dangling entries.
    await session.execute(
        sql_delete(Notification).where(
            Notification.kind == NotificationKind.announcement,
            Notification.payload["announcement_id"].astext == str(a.id),
        )
    )
    await session.delete(a)


async def _fanout_notifications(a: Announcement, session) -> None:
    """Create one Notification per recipient based on audience."""
    recipients_stmt = select(User.id).where(
        User.tenant_id == a.tenant_id,
        User.is_active.is_(True),
    )

    if a.audience is AnnouncementAudience.teachers:
        recipients_stmt = recipients_stmt.where(User.role == UserRole.teacher)
    elif a.audience is AnnouncementAudience.parents:
        recipients_stmt = recipients_stmt.where(User.role == UserRole.parent)
    elif a.audience is AnnouncementAudience.students:
        recipients_stmt = recipients_stmt.where(User.role == UserRole.student)
    elif a.audience is AnnouncementAudience.classroom and a.classroom_id is not None:
        # Parents of students in this classroom + the student users in it +
        # all teachers in the tenant (they get a broader feed).
        parent_ids_subq = (
            select(ParentStudent.parent_id)
            .join(Student, Student.id == ParentStudent.student_id)
            .where(Student.classroom_id == a.classroom_id)
        )
        student_user_ids_subq = (
            select(Student.user_id).where(
                Student.classroom_id == a.classroom_id,
                Student.user_id.isnot(None),
            )
        )
        recipients_stmt = recipients_stmt.where(
            or_(
                User.id.in_(parent_ids_subq),
                User.id.in_(student_user_ids_subq),
                User.role == UserRole.teacher,
            )
        )
    # school: everyone — no extra filter

    user_ids = (await session.execute(recipients_stmt)).scalars().all()
    for uid in user_ids:
        if uid == a.author_id:
            continue
        session.add(
            Notification(
                user_id=uid,
                kind=NotificationKind.announcement,
                title=a.title,
                body=a.body[:280],
                link=f"/announcements/{a.id}",
                payload={"announcement_id": str(a.id)},
            )
        )
