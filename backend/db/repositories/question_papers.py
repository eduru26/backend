from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Assessment, QuestionPaperTemplate


class TemplateRepository:
    """CRUD for school-admin question-paper templates."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_templates(
        self, tenant_id: uuid.UUID, *, exam_type: str | None = None
    ) -> list[QuestionPaperTemplate]:
        stmt = select(QuestionPaperTemplate).where(
            QuestionPaperTemplate.tenant_id == tenant_id
        )
        if exam_type:
            stmt = stmt.where(QuestionPaperTemplate.exam_type == exam_type)
        stmt = stmt.order_by(QuestionPaperTemplate.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(
        self, template_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> QuestionPaperTemplate | None:
        result = await self.session.execute(
            select(QuestionPaperTemplate).where(
                QuestionPaperTemplate.id == template_id,
                QuestionPaperTemplate.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        exam_type: str,
        total_marks: Decimal,
        sections: list,
        created_by: uuid.UUID | None,
        duration_minutes: int | None = None,
    ) -> QuestionPaperTemplate:
        tpl = QuestionPaperTemplate(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name.strip(),
            exam_type=exam_type,
            total_marks=total_marks,
            sections=sections,
            created_by=created_by,
            duration_minutes=duration_minutes,
        )
        self.session.add(tpl)
        await self.session.flush()
        return tpl

    async def update(
        self,
        tpl: QuestionPaperTemplate,
        *,
        name: str | None = None,
        total_marks: Decimal | None = None,
        sections: list | None = None,
        is_active: bool | None = None,
        duration_minutes: int | None = None,
        # Sentinel: pass explicit `None` via duration_minutes_explicit
        # to clear the field (since None means "no change" above).
        duration_minutes_explicit: bool = False,
    ) -> QuestionPaperTemplate:
        if name is not None:
            tpl.name = name.strip()
        if total_marks is not None:
            tpl.total_marks = total_marks
        if sections is not None:
            tpl.sections = sections
        if is_active is not None:
            tpl.is_active = is_active
        if duration_minutes is not None or duration_minutes_explicit:
            tpl.duration_minutes = duration_minutes
        await self.session.flush()
        return tpl

    async def delete(self, tpl: QuestionPaperTemplate) -> None:
        await self.session.delete(tpl)
        await self.session.flush()

    async def is_referenced(self, template_id: uuid.UUID) -> bool:
        """True if any assessment was generated from this template — such a
        template must not be deleted."""
        result = await self.session.execute(
            select(Assessment.id).where(Assessment.template_id == template_id).limit(1)
        )
        return result.scalar_one_or_none() is not None
