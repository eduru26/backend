"""End-to-end smoke test for the Question Papers feature.

Exercises: curriculum (chapter+topics) -> template -> AI paper generation
-> finalize -> marks -> report card -> chapter analytics rollup.

Run inside the backend container:
    docker compose exec -T backend python //app/scripts/smoke_qp.py
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select

from backend.db.base import get_session_factory
from backend.db.models import (
    Assessment, AssessmentType, Classroom, Question, QuestionPaperTemplate,
    QuestionResponse, Student, Subject, Tenant, User, UserRole,
)
from backend.db.repositories.curriculum import ChapterRepository
from backend.services.paper_generator import PaperGenerator


async def main() -> None:
    factory = get_session_factory()
    async with factory() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == "st-paul-hsc")
        )).scalar_one_or_none() or (await s.execute(select(Tenant).limit(1))).scalar_one()
        # a classroom that has students
        classroom = None
        for c in (await s.execute(
            select(Classroom).where(Classroom.tenant_id == tenant.id)
        )).scalars().all():
            n = len((await s.execute(
                select(Student.id).where(Student.classroom_id == c.id)
            )).scalars().all())
            if n:
                classroom = c
                break
        if classroom is None:
            print("no classroom with students — abort"); return
        students = (await s.execute(
            select(Student).where(Student.classroom_id == classroom.id)
        )).scalars().all()
        subject = (await s.execute(select(Subject).limit(1))).scalar_one_or_none()
        if subject is None:
            subject = Subject(id=uuid.uuid4(), name="Science")
            s.add(subject); await s.flush()
        teacher = (await s.execute(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.teacher).limit(1)
        )).scalar_one()
        print(f"[setup] tenant={tenant.name} class={classroom.name} "
              f"students={len(students)} subject={subject.name}")

        # 1. curriculum
        repo = ChapterRepository(s)
        chapter = await repo.create_chapter(
            tenant_id=tenant.id, subject_id=subject.id, grade=classroom.grade,
            name=f"SmokeTest Chapter {uuid.uuid4().hex[:6]}",
        )
        t1 = await repo.upsert_topic_in_chapter(
            subject_id=subject.id, chapter_id=chapter.id, name=f"SmokeTopic A {uuid.uuid4().hex[:4]}")
        t2 = await repo.upsert_topic_in_chapter(
            subject_id=subject.id, chapter_id=chapter.id, name=f"SmokeTopic B {uuid.uuid4().hex[:4]}")
        print(f"[curriculum] chapter + 2 topics created")

        # 2. template — one short-answer section, 3 questions x 2 marks
        template = QuestionPaperTemplate(
            id=uuid.uuid4(), tenant_id=tenant.id, name=f"SmokeTpl {uuid.uuid4().hex[:6]}",
            exam_type="unit_test", total_marks=Decimal("6"),
            sections=[{
                "section_no": 1, "title": "Section A", "question_type": "short_answer",
                "question_count": 3, "marks_per_question": 2, "optional_of": None,
            }],
            created_by=teacher.id,
        )
        s.add(template); await s.flush()
        print(f"[template] created ({template.total_marks} marks)")

        # 3. assessment + generation
        assessment = Assessment(
            id=uuid.uuid4(), type=AssessmentType.unit_test,
            title=f"{subject.name} Unit Test — {classroom.name}",
            subject_id=subject.id, assessment_date=date.today(),
            max_marks=Decimal("6"), created_by=teacher.id,
            classroom_id=classroom.id, template_id=template.id, status="generating",
            generation_meta={
                "tenant_id": str(tenant.id), "template_id": str(template.id),
                "subject_id": str(subject.id), "subject_name": subject.name,
                "grade": classroom.grade, "classroom_id": str(classroom.id),
                "chapter_ids": [str(chapter.id)],
                "topic_ids": [str(t1.id), str(t2.id)],
                "exam_type": "unit_test", "weightage": {}, "created_by": str(teacher.id),
            },
        )
        s.add(assessment); await s.commit()

    # generation (own session — calls Ollama)
    print("[generate] running PaperGenerator (calls the LLM)…")
    async with factory() as s:
        await PaperGenerator(s).generate(assessment.id)
        await s.commit()

    async with factory() as s:
        a = await s.get(Assessment, assessment.id)
        qs = (await s.execute(
            select(Question).where(Question.assessment_id == assessment.id)
            .order_by(Question.question_no)
        )).scalars().all()
        print(f"[generate] status={a.status}  questions={len(qs)}")
        for q in qs[:3]:
            txt = (q.question_text or "")[:90]
            print(f"  Q{q.question_no} [{q.difficulty.value}] {q.max_marks}m: {txt}")
        if not qs:
            print("  WARNING: no questions generated");

        # 4. finalize + marks
        a.status = "finalized"
        for st in students[:3]:
            for i, q in enumerate(qs):
                marks = Decimal("2") if i == 0 else Decimal("1")
                s.add(QuestionResponse(
                    id=uuid.uuid4(), question_id=q.id, student_id=st.id,
                    marks_obtained=marks, is_correct=marks >= q.max_marks,
                ))
        await s.commit()
        print(f"[marks] entered for {min(3, len(students))} students")

    # 5. report + chapter analytics
    async with factory() as s:
        from backend.agents.context import ContextAgent
        from backend.agents.analytics import AnalyticsAgent
        st = students[0]
        ctx = ContextAgent(s)
        scores = await ctx._get_scores(st.id)  # type: ignore[attr-defined]
        ch_rows = [r for r in scores if r.get("chapter")]
        print(f"[analytics] student {st.handle}: {len(scores)} score rows, "
              f"{len(ch_rows)} with a chapter")
        analytics = AnalyticsAgent()._student_analytics(scores, [])  # type: ignore[attr-defined]
        print(f"[analytics] chapter_pct={analytics.get('chapter_pct')}")
        print(f"[analytics] subject_pct={analytics.get('subject_pct')}")

    print("\nQUESTION PAPER SMOKE TEST COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
