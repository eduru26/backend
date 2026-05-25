from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Chapter, Topic


class ChapterRepository:
    """Data access for the Subject -> Chapter -> Topic curriculum tree.

    Chapters are tenant-scoped. Topics stay global-by-subject (the existing
    `uq_topics_subject_name` constraint); a topic joins a school's tree via
    its nullable `chapter_id`. This matches the single-school-per-deployment
    model — see docs/hardware-capacity.md.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── chapters ───────────────────────────────────────────

    async def list_chapters(
        self, *, tenant_id: uuid.UUID, subject_id: uuid.UUID, grade: int
    ) -> list[Chapter]:
        result = await self.session.execute(
            select(Chapter)
            .where(
                Chapter.tenant_id == tenant_id,
                Chapter.subject_id == subject_id,
                Chapter.grade == grade,
            )
            .order_by(Chapter.order_num, Chapter.name)
        )
        return list(result.scalars().all())

    async def get_chapter(
        self, chapter_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Chapter | None:
        result = await self.session.execute(
            select(Chapter).where(
                Chapter.id == chapter_id, Chapter.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def create_chapter(
        self,
        *,
        tenant_id: uuid.UUID,
        subject_id: uuid.UUID,
        grade: int,
        name: str,
        order_num: int = 0,
        source: str = "manual",
    ) -> Chapter:
        chapter = Chapter(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            subject_id=subject_id,
            grade=grade,
            name=name.strip(),
            order_num=order_num,
            source=source,
        )
        self.session.add(chapter)
        await self.session.flush()
        return chapter

    async def update_chapter(
        self,
        chapter: Chapter,
        *,
        name: str | None = None,
        order_num: int | None = None,
    ) -> Chapter:
        if name is not None:
            chapter.name = name.strip()
        if order_num is not None:
            chapter.order_num = order_num
        await self.session.flush()
        return chapter

    async def delete_chapter(self, chapter: Chapter) -> None:
        # Topic.chapter_id is ON DELETE SET NULL — topics survive, detached.
        await self.session.delete(chapter)
        await self.session.flush()

    # ── topics ─────────────────────────────────────────────

    async def topics_in_chapter(self, chapter_id: uuid.UUID) -> list[Topic]:
        result = await self.session.execute(
            select(Topic).where(Topic.chapter_id == chapter_id).order_by(Topic.name)
        )
        return list(result.scalars().all())

    async def upsert_topic_in_chapter(
        self, *, subject_id: uuid.UUID, chapter_id: uuid.UUID, name: str
    ) -> Topic:
        """Create (or reuse) a topic and link it to the chapter.

        Topics are global per subject; if one already exists for
        (subject_id, name) we reuse it and just (re)assign its chapter.
        """
        name = name.strip()
        await self.session.execute(
            pg_insert(Topic)
            .values(subject_id=subject_id, name=name, chapter_id=chapter_id)
            .on_conflict_do_nothing(constraint="uq_topics_subject_name")
        )
        result = await self.session.execute(
            select(Topic).where(
                Topic.subject_id == subject_id, Topic.name == name
            )
        )
        topic = result.scalar_one()
        if topic.chapter_id != chapter_id:
            topic.chapter_id = chapter_id
            await self.session.flush()
        return topic

    async def get_topic(self, topic_id: uuid.UUID) -> Topic | None:
        return await self.session.get(Topic, topic_id)

    async def move_topic(self, topic: Topic, chapter_id: uuid.UUID | None) -> Topic:
        topic.chapter_id = chapter_id
        await self.session.flush()
        return topic
