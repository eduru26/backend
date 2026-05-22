"""Background scheduler that emits class_reminder notifications 5 min before each class.

Runs every minute. For each timetable entry whose start_time falls in
[now + LEAD_MIN, now + LEAD_MIN + 1 min) on today's day-of-week, we insert a
Notification for the teacher. A natural-key check on (user_id, payload key,
created today) prevents duplicates if the loop runs multiple times.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.db.base import get_session_factory
from backend.db.models import (
    Classroom, Notification, NotificationKind, PeriodTime, TimetableEntry,
)

logger = get_logger(__name__)

LEAD_MIN = 5            # how many minutes before class to fire the reminder
TICK_SEC = 60           # poll interval


async def _emit_due_reminders(session: AsyncSession, now: datetime) -> int:
    """Insert reminders for classes starting in [now+LEAD, now+LEAD+1min)."""
    today_dow = now.isoweekday()  # 1=Mon … 7=Sun
    if today_dow > 6:  # No Sunday classes by convention (days are 1..6)
        return 0
    today_day_type = "saturday" if today_dow == 6 else "weekday"

    window_start = (now + timedelta(minutes=LEAD_MIN)).time()
    window_end = (now + timedelta(minutes=LEAD_MIN + 1)).time()

    # Find entries whose period_time.start_time falls in [window_start, window_end)
    q = (
        select(TimetableEntry, PeriodTime, Classroom)
        .join(
            PeriodTime,
            and_(
                PeriodTime.tenant_id == TimetableEntry.tenant_id,
                PeriodTime.period_number == TimetableEntry.period_number,
                PeriodTime.day_type == today_day_type,
            ),
        )
        .outerjoin(Classroom, Classroom.id == TimetableEntry.classroom_id)
        .where(
            TimetableEntry.day_of_week == today_dow,
            PeriodTime.start_time >= window_start,
            PeriodTime.start_time < window_end,
        )
    )
    rows = (await session.execute(q)).all()
    if not rows:
        return 0

    today_iso = date.today().isoformat()
    inserted = 0
    for entry, pt, cls in rows:
        natural_key = f"class_reminder:{today_iso}:{entry.id}"
        existing = await session.scalar(
            select(Notification.id).where(
                Notification.user_id == entry.teacher_id,
                Notification.kind == NotificationKind.class_reminder,
                Notification.payload["key"].astext == natural_key,
            )
        )
        if existing:
            continue
        cls_label = cls.name if cls else "—"
        body = (
            f"{entry.subject} · {cls_label} starts at "
            f"{pt.start_time.strftime('%H:%M')} (period {entry.period_number})."
        )
        session.add(
            Notification(
                user_id=entry.teacher_id,
                kind=NotificationKind.class_reminder,
                title="Class starting in 5 min",
                body=body,
                link="/dashboard",
                payload={
                    "key": natural_key,
                    "timetable_entry_id": str(entry.id),
                    "subject": entry.subject,
                    "classroom_id": str(entry.classroom_id) if entry.classroom_id else None,
                    "period": entry.period_number,
                    "start_time": pt.start_time.isoformat(),
                },
            )
        )
        inserted += 1
    if inserted:
        await session.commit()
    return inserted


async def _run_loop() -> None:
    factory = get_session_factory()
    logger.info("class_reminders.start", lead_min=LEAD_MIN, tick_sec=TICK_SEC)
    while True:
        try:
            async with factory() as session:
                n = await _emit_due_reminders(session, datetime.now(timezone.utc))
                if n:
                    logger.info("class_reminders.emitted", count=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("class_reminders.error", error=str(exc))
        await asyncio.sleep(TICK_SEC)


def start(loop_task_holder: dict) -> asyncio.Task:
    task = asyncio.create_task(_run_loop(), name="class_reminders")
    loop_task_holder["class_reminders"] = task
    return task


async def stop(loop_task_holder: dict) -> None:
    task = loop_task_holder.get("class_reminders")
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
