from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import QuestionResponse


class ResponseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_upsert(self, rows: Iterable[dict]) -> int:
        """Upsert by (question_id, student_id). Re-uploads correct marks."""
        rows_list = list(rows)
        if not rows_list:
            return 0

        stmt = pg_insert(QuestionResponse).values(rows_list)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_responses_question_student",
            set_={
                "marks_obtained": stmt.excluded.marks_obtained,
                "is_correct": stmt.excluded.is_correct,
            },
        )
        await self.session.execute(stmt)
        return len(rows_list)
