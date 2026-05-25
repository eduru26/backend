from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Assessment, Attendance, Chapter, ParentStudent, Question, QuestionResponse,
    Student, Subject, Topic,
)
from backend.agents.state import MentorState


class ContextAgent:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, state: MentorState) -> dict[str, Any]:
        role = state["role"]
        tid = uuid.UUID(state["tenant_id"])
        if role == "student":
            return await self._student_context(state, tid)
        if role == "parent":
            return await self._parent_context(state, tid)
        return await self._teacher_context(state, tid)

    # ── per-role fetchers ───────────────────────────────────

    async def _student_context(self, state: MentorState, tid: uuid.UUID) -> dict[str, Any]:
        sid_str = state.get("student_id")
        if not sid_str:
            return {"students": [], "raw_scores": [], "raw_attendance": []}
        sid = uuid.UUID(sid_str)
        student = await self._get_student(sid, tid)
        return {
            "students": [student] if student else [],
            "raw_scores": await self._get_scores(sid),
            "raw_attendance": await self._get_attendance(sid),
        }

    async def _parent_context(self, state: MentorState, tid: uuid.UUID) -> dict[str, Any]:
        # Children are linked via parent_students (NOT Student.parent_email —
        # that column was dropped in migration 0008). Scope to this tenant too
        # so a malformed link from another school can't leak data.
        parent_uid = uuid.UUID(state["user_id"])
        result = await self._session.execute(
            select(Student)
            .join(ParentStudent, ParentStudent.student_id == Student.id)
            .where(
                ParentStudent.parent_id == parent_uid,
                Student.tenant_id == tid,
            )
        )
        children = list(result.scalars().all())

        target_sid = state.get("student_id")
        if target_sid:
            children = [c for c in children if str(c.id) == target_sid]

        students = [
            {"id": str(c.id), "name": c.full_name, "grade": c.grade, "section": c.section}
            for c in children
        ]
        all_scores: list[dict[str, Any]] = []
        all_att: list[dict[str, Any]] = []
        for child in children:
            all_scores.extend(await self._get_scores(child.id))
            all_att.extend(await self._get_attendance(child.id))
        return {"students": students, "raw_scores": all_scores, "raw_attendance": all_att}

    async def _teacher_context(self, state: MentorState, tid: uuid.UUID) -> dict[str, Any]:
        # Teachers only see students in their own school
        target_sid = state.get("student_id")
        if target_sid:
            # Focused on one student
            sid = uuid.UUID(target_sid)
            student = await self._get_student(sid, tid)
            students = [student] if student else []
            all_scores = await self._get_scores(sid) if student else []
            all_att = await self._get_attendance(sid) if student else []
        else:
            # No student_id → "whole class" scope. If the request named a
            # classroom, restrict to its students; otherwise fall back to the
            # whole tenant (legacy behaviour).
            stmt = select(Student).where(Student.tenant_id == tid)
            cid_str = state.get("classroom_id")
            if cid_str:
                try:
                    cid = uuid.UUID(cid_str)
                    stmt = stmt.where(Student.classroom_id == cid)
                except ValueError:
                    pass  # bad UUID → ignore filter, behave like whole-tenant
            result = await self._session.execute(stmt.order_by(Student.full_name))
            all_students = list(result.scalars().all())
            students = [
                {"id": str(s.id), "name": s.full_name, "grade": s.grade, "section": s.section}
                for s in all_students
            ]
            all_scores: list[dict[str, Any]] = []
            all_att: list[dict[str, Any]] = []
            for s in all_students:
                all_scores.extend(await self._get_scores(s.id))
                all_att.extend(await self._get_attendance(s.id))

        return {"students": students, "raw_scores": all_scores, "raw_attendance": all_att}

    # ── DB query helpers ────────────────────────────────────

    async def _get_student(self, student_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(Student).where(
                Student.id == student_id,
                Student.tenant_id == tenant_id,
            )
        )
        s = result.scalar_one_or_none()
        if not s:
            return None
        return {"id": str(s.id), "name": s.full_name, "grade": s.grade, "section": s.section}

    async def _get_scores(self, student_id: uuid.UUID) -> list[dict[str, Any]]:
        stmt = (
            select(
                QuestionResponse.marks_obtained,
                Question.max_marks,
                Topic.name.label("topic"),
                Chapter.name.label("chapter"),
                Subject.name.label("subject"),
                Assessment.assessment_date,
                Assessment.title.label("assessment_title"),
            )
            .join(Question, QuestionResponse.question_id == Question.id)
            .join(Topic, Question.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            # LEFT JOIN — topics without a chapter still contribute scores.
            .outerjoin(Chapter, Topic.chapter_id == Chapter.id)
            .join(Assessment, Question.assessment_id == Assessment.id)
            .where(QuestionResponse.student_id == student_id)
            .order_by(Assessment.assessment_date)
        )
        result = await self._session.execute(stmt)
        return [
            {
                "student_id": str(student_id),
                "subject": r.subject,
                "topic": r.topic,
                "chapter": r.chapter,   # None for unassigned topics
                "marks_obtained": float(r.marks_obtained),
                "max_marks": float(r.max_marks),
                "date": str(r.assessment_date),
                "assessment_title": r.assessment_title,
            }
            for r in result.all()
        ]

    async def _get_attendance(self, student_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(Attendance)
            .where(Attendance.student_id == student_id)
            .order_by(Attendance.date)
        )
        return [
            {"student_id": str(student_id), "date": str(r.date), "status": r.status.value}
            for r in result.scalars().all()
        ]
