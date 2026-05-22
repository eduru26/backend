"""Helpers to emit notifications. Used by ingest and teacher endpoints."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Attendance, AttendanceStatus, Notification, NotificationKind,
    ParentStudent, Student,
)


async def _student_recipients(session: AsyncSession, student_id: uuid.UUID) -> list[uuid.UUID]:
    """Return user ids to notify for events about a given student:
    the student's own dedicated User row (role='student') and every linked
    parent via ParentStudent. Both target the same household; we include
    both so a student logged in via roll-number sees the notification too.
    """
    parents = (await session.execute(
        select(ParentStudent.parent_id).where(ParentStudent.student_id == student_id)
    )).scalars().all()
    student_uid = await session.scalar(
        select(Student.user_id).where(Student.id == student_id)
    )
    ids: set[uuid.UUID] = set(parents)
    if student_uid is not None:
        ids.add(student_uid)
    return list(ids)


async def _classroom_recipients(
    session: AsyncSession,
    classroom_id: uuid.UUID,
) -> list[uuid.UUID]:
    """All household users (parents + student users) linked to a classroom."""
    parents = (await session.execute(
        select(ParentStudent.parent_id)
        .join(Student, Student.id == ParentStudent.student_id)
        .where(Student.classroom_id == classroom_id)
        .distinct()
    )).scalars().all()
    student_uids = (await session.execute(
        select(Student.user_id).where(
            Student.classroom_id == classroom_id,
            Student.user_id.isnot(None),
        )
    )).scalars().all()
    return list({*parents, *student_uids})


async def notify_marks_posted(
    session: AsyncSession,
    student_ids: set[uuid.UUID],
    assessment_title: str,
) -> None:
    """One notification per affected student's household (parent + student)."""
    for sid in student_ids:
        for uid in await _student_recipients(session, sid):
            session.add(
                Notification(
                    user_id=uid,
                    kind=NotificationKind.mark_posted,
                    title="New marks posted",
                    body=f"New results are available for: {assessment_title}",
                    link="/dashboard",
                    payload={"student_id": str(sid)},
                )
            )


async def notify_assignment_published(
    session: AsyncSession,
    classroom_id: uuid.UUID,
    assignment_title: str,
    assignment_id: uuid.UUID,
) -> None:
    for uid in await _classroom_recipients(session, classroom_id):
        session.add(
            Notification(
                user_id=uid,
                kind=NotificationKind.assignment_published,
                title="New assignment",
                body=assignment_title,
                link="/assignments",
                payload={"assignment_id": str(assignment_id)},
            )
        )


ATTENDANCE_ALERT_THRESHOLD = 75.0  # percentage


async def maybe_alert_low_attendance(
    session: AsyncSession,
    student_ids: set[uuid.UUID],
) -> None:
    """Compute rolling 30-day attendance per student; alert if below threshold."""
    if not student_ids:
        return
    cutoff = date.today() - timedelta(days=30)

    # Group attendance counts by student and status
    rows = (await session.execute(
        select(
            Attendance.student_id,
            Attendance.status,
            func.count(Attendance.id),
        )
        .where(
            Attendance.student_id.in_(student_ids),
            Attendance.date >= cutoff,
        )
        .group_by(Attendance.student_id, Attendance.status)
    )).all()

    by_student: dict[uuid.UUID, dict[AttendanceStatus, int]] = {}
    for sid, status, n in rows:
        by_student.setdefault(sid, {})[status] = int(n)

    for sid, counts in by_student.items():
        present = counts.get(AttendanceStatus.present, 0)
        late = counts.get(AttendanceStatus.late, 0)
        absent = counts.get(AttendanceStatus.absent, 0)
        total = present + late + absent
        if total < 5:
            continue
        rate = (present + late) / total * 100
        if rate >= ATTENDANCE_ALERT_THRESHOLD:
            continue

        # Avoid spamming: only emit if no attendance_alert about this student
        # in the last 7 days. Match on payload.student_id (covers both parent
        # and student-user recipients in one check).
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = await session.scalar(
            select(Notification.id).where(
                Notification.kind == NotificationKind.attendance_alert,
                Notification.created_at >= recent_cutoff,
                Notification.payload["student_id"].astext == str(sid),
            ).limit(1)
        )
        if recent:
            continue

        recipients = await _student_recipients(session, sid)
        rounded = round(rate, 1)
        for uid in recipients:
            session.add(
                Notification(
                    user_id=uid,
                    kind=NotificationKind.attendance_alert,
                    title="Attendance alert",
                    body=f"Attendance over the last 30 days is {rounded}% — below the school’s threshold.",
                    link="/dashboard",
                    payload={"student_id": str(sid), "attendance_pct": rounded},
                )
            )
