from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Student


class StudentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, student_id: uuid.UUID) -> Student | None:
        return await self.session.get(Student, student_id)

    async def get_by_handle(self, handle: str, tenant_id: uuid.UUID) -> Student | None:
        result = await self.session.execute(
            select(Student).where(
                Student.handle == handle,
                Student.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_handle_global(self, handle: str) -> Student | None:
        """Find a student by roll number across all tenants. Returns None if not found or ambiguous."""
        result = await self.session.execute(
            select(Student).where(Student.handle == handle)
        )
        rows = result.scalars().all()
        if len(rows) == 1:
            return rows[0]
        return None

    async def list_all(self, tenant_id: uuid.UUID) -> list[Student]:
        result = await self.session.execute(
            select(Student)
            .where(Student.tenant_id == tenant_id)
            .order_by(Student.handle)
        )
        return list(result.scalars().all())

    async def upsert_many(self, rows: Iterable[dict], tenant_id: uuid.UUID) -> int:
        """Bulk upsert students scoped to a tenant. Returns number of rows processed.

        Accepts a `parent_phone` column on each row. Older callers may still
        send `parent_email`; we rename it transparently because the
        `parent_email` column was dropped in migration 0008.
        """
        rows_list: list[dict] = []
        for row in rows:
            normalized = {**row}
            if "parent_email" in normalized and "parent_phone" not in normalized:
                normalized["parent_phone"] = normalized.pop("parent_email")
            else:
                normalized.pop("parent_email", None)
            normalized["id"] = uuid.uuid4()  # ignored on conflict; required for insert
            normalized["tenant_id"] = tenant_id
            rows_list.append(normalized)

        if not rows_list:
            return 0

        stmt = pg_insert(Student).values(rows_list)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_students_tenant_handle",
            set_={
                "full_name": stmt.excluded.full_name,
                "grade": stmt.excluded.grade,
                "section": stmt.excluded.section,
                "parent_phone": stmt.excluded.parent_phone,
            },
        )
        await self.session.execute(stmt)
        return len(rows_list)
