from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Assessment, AssessmentType, Question


class AssessmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, assessment_id: uuid.UUID) -> Assessment | None:
        return await self.session.get(Assessment, assessment_id)

    async def create(
        self,
        *,
        type_: AssessmentType,
        title: str,
        subject_id: uuid.UUID,
        assessment_date: date,
        max_marks: Decimal,
        created_by: uuid.UUID,
    ) -> Assessment:
        assessment = Assessment(
            type=type_,
            title=title,
            subject_id=subject_id,
            assessment_date=assessment_date,
            max_marks=max_marks,
            created_by=created_by,
        )
        self.session.add(assessment)
        await self.session.flush()
        await self.session.refresh(assessment)
        return assessment

    async def bulk_create_questions(
        self, rows: Iterable[dict]
    ) -> list[Question]:
        rows_list = list(rows)
        if not rows_list:
            return []
        questions = [Question(**row) for row in rows_list]
        self.session.add_all(questions)
        await self.session.flush()
        return questions

    async def find_question(
        self, *, assessment_id: uuid.UUID, question_no: int
    ) -> Question | None:
        result = await self.session.execute(
            select(Question).where(
                Question.assessment_id == assessment_id,
                Question.question_no == question_no,
            )
        )
        return result.scalar_one_or_none()

    async def map_question_nos(
        self, assessment_id: uuid.UUID
    ) -> dict[int, Question]:
        result = await self.session.execute(
            select(Question).where(Question.assessment_id == assessment_id)
        )
        return {q.question_no: q for q in result.scalars().all()}
