from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Subject, Topic


class SubjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_subject(self, name: str) -> Subject:
        stmt = (
            pg_insert(Subject)
            .values(name=name)
            .on_conflict_do_nothing(index_elements=[Subject.name])
        )
        await self.session.execute(stmt)
        result = await self.session.execute(
            select(Subject).where(Subject.name == name)
        )
        return result.scalar_one()

    async def upsert_topic(self, *, subject_id: uuid.UUID, name: str) -> Topic:
        stmt = (
            pg_insert(Topic)
            .values(subject_id=subject_id, name=name)
            .on_conflict_do_nothing(constraint="uq_topics_subject_name")
        )
        await self.session.execute(stmt)
        result = await self.session.execute(
            select(Topic).where(
                Topic.subject_id == subject_id,
                Topic.name == name,
            )
        )
        return result.scalar_one()

    async def list_topics_for_subject(self, subject_id: uuid.UUID) -> list[Topic]:
        result = await self.session.execute(
            select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.name)
        )
        return list(result.scalars().all())
