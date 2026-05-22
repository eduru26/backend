from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Attendance


class AttendanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_upsert(self, rows: Iterable[dict]) -> int:
        rows_list = list(rows)
        if not rows_list:
            return 0

        stmt = pg_insert(Attendance).values(rows_list)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_attendance_student_date",
            set_={"status": stmt.excluded.status},
        )
        await self.session.execute(stmt)
        return len(rows_list)
