from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Classroom,
    ParentStudent,
    Question,
    QuestionResponse,
    Student,
    TeacherSubjectAssignment,
    Topic,
    User,
)
from backend.rag.identity import IdentityClaims


@dataclass(frozen=True)
class KGNode:
    kind: str        # "student" | "teacher" | "classroom" | "subject" | "topic" | "parent"
    id: str
    label: str
    extra: dict


@dataclass(frozen=True)
class KGEdge:
    src: KGNode
    rel: str         # "enrolled_in" | "taught_by" | "has_parent" | "weak_in" | "covers"
    dst: KGNode


@dataclass(frozen=True)
class KGNeighbourhood:
    center: KGNode
    edges: tuple[KGEdge, ...]

    def by_relation(self, rel: str) -> tuple[KGEdge, ...]:
        return tuple(e for e in self.edges if e.rel == rel)


class KnowledgeGraph:
    """Read-only graph over existing SQL FKs.

    This is *not* a separate graph database. It's a typed traversal API
    that wraps the existing relational schema as nodes + edges, with
    every query gated by `IdentityClaims` so a traversal can't leak
    cross-tenant rows. The point is to give the prompt builder
    structured neighbourhood information ("this student is in 8A, taught
    by Smith for Math, weakest topic is Algebra") in one call instead of
    five.

    When the data grows past what relational joins handle well, swap the
    backing store; the public surface (`neighbourhood`, `weakest_topics`,
    `at_risk_in_classroom`) stays the same.
    """

    WEAK_TOPIC_PCT = 60.0
    AT_RISK_OVERALL_PCT = 50.0

    def __init__(self, session: AsyncSession, identity: IdentityClaims) -> None:
        self._session = session
        self._id = identity

    # ── public traversals ──────────────────────────────────

    async def neighbourhood(
        self, *, student_id: uuid.UUID, hops: int = 1
    ) -> KGNeighbourhood:
        """One- or two-hop neighbourhood around a student node.

        Hops covered:
          1: classroom, parents, weak topics
          2: teachers (via classroom), classmates (via classroom)
        """
        # Enforce scope first.
        if not self._id.may_see_student(student_id):
            raise PermissionError(f"caller may not see student {student_id}")

        s = await self._session.get(Student, student_id)
        if s is None or s.tenant_id != self._id.tenant_id:
            raise PermissionError("student not in tenant")

        center = KGNode(
            kind="student", id=str(s.id), label=s.full_name,
            extra={"grade": s.grade, "section": s.section, "handle": s.handle},
        )
        edges: list[KGEdge] = []

        # — classroom
        if s.classroom_id:
            cls = await self._session.get(Classroom, s.classroom_id)
            if cls is not None:
                cls_node = KGNode("classroom", str(cls.id), cls.name,
                                  {"grade": cls.grade, "section": cls.section})
                edges.append(KGEdge(center, "enrolled_in", cls_node))

                if hops >= 2:
                    # teachers of this classroom
                    if cls.teacher_id:
                        teacher = await self._session.get(User, cls.teacher_id)
                        if teacher is not None:
                            edges.append(KGEdge(
                                cls_node, "taught_by",
                                KGNode("teacher", str(teacher.id),
                                       teacher.full_name or teacher.email or "Teacher",
                                       {"role": "class_teacher"}),
                            ))
                    sub_teachers = await self._session.execute(
                        select(TeacherSubjectAssignment, User)
                        .join(User, User.id == TeacherSubjectAssignment.teacher_id)
                        .where(TeacherSubjectAssignment.classroom_id == cls.id)
                    )
                    for tsa, teacher in sub_teachers.all():
                        edges.append(KGEdge(
                            cls_node, "taught_by",
                            KGNode("teacher", str(teacher.id),
                                   teacher.full_name or teacher.email or "Teacher",
                                   {"subject": tsa.subject}),
                        ))

                    # classmates (cap to avoid blowing the prompt; we only
                    # need a few names for "students like this one")
                    classmates = await self._session.execute(
                        select(Student).where(
                            Student.classroom_id == cls.id,
                            Student.id != s.id,
                            Student.tenant_id == self._id.tenant_id,
                        ).limit(8)
                    )
                    for cm in classmates.scalars().all():
                        edges.append(KGEdge(
                            cls_node, "has_classmate",
                            KGNode("student", str(cm.id), cm.full_name,
                                   {"handle": cm.handle}),
                        ))

        # — parents (only surfaced to teacher/school_admin — for student/
        #   parent it would be self-referential noise)
        if self._id.role.value in ("teacher", "school_admin"):
            parents = await self._session.execute(
                select(User)
                .join(ParentStudent, ParentStudent.parent_id == User.id)
                .where(ParentStudent.student_id == s.id)
            )
            for p in parents.scalars().all():
                edges.append(KGEdge(
                    center, "has_parent",
                    KGNode("parent", str(p.id), p.full_name or "Parent",
                           {"phone": p.phone}),
                ))

        # — weak topics (derived: per-topic accuracy < threshold)
        weak = await self.weakest_topics(student_id=s.id, limit=5)
        for topic_label, pct in weak:
            edges.append(KGEdge(
                center, "weak_in",
                KGNode("topic", topic_label, topic_label, {"accuracy_pct": pct}),
            ))

        return KGNeighbourhood(center=center, edges=tuple(edges))

    async def weakest_topics(
        self, *, student_id: uuid.UUID, limit: int = 5
    ) -> list[tuple[str, float]]:
        """Returns [(topic_label, accuracy_pct)] sorted ascending by accuracy."""
        if not self._id.may_see_student(student_id):
            return []
        rows = await self._session.execute(
            select(
                Topic.name.label("topic_name"),
                QuestionResponse.marks_obtained,
                Question.max_marks,
            )
            .join(Question, QuestionResponse.question_id == Question.id)
            .join(Topic, Question.topic_id == Topic.id)
            .where(QuestionResponse.student_id == student_id)
        )
        agg: dict[str, list[float]] = {}
        for r in rows.all():
            obt = float(r.marks_obtained)
            mx = float(r.max_marks) or 1.0
            agg.setdefault(r.topic_name, []).append(obt / mx * 100.0)
        scored = [(t, round(sum(v) / len(v), 1)) for t, v in agg.items() if v]
        scored = [s for s in scored if s[1] < self.WEAK_TOPIC_PCT]
        scored.sort(key=lambda x: x[1])
        return scored[:limit]

    async def at_risk_in_classroom(
        self, *, classroom_id: uuid.UUID, limit: int = 10
    ) -> list[KGNode]:
        """Students in a classroom whose overall accuracy is below threshold."""
        if not self._id.may_see_classroom(classroom_id):
            return []
        students = await self._session.execute(
            select(Student).where(
                Student.classroom_id == classroom_id,
                Student.tenant_id == self._id.tenant_id,
            )
        )
        out: list[KGNode] = []
        for s in students.scalars().all():
            overall = await self._overall_pct(s.id)
            if overall is not None and overall < self.AT_RISK_OVERALL_PCT:
                out.append(KGNode(
                    "student", str(s.id), s.full_name,
                    {"overall_pct": overall, "handle": s.handle},
                ))
        out.sort(key=lambda n: n.extra["overall_pct"])
        return out[:limit]

    # ── private helpers ────────────────────────────────────

    async def _overall_pct(self, student_id: uuid.UUID) -> float | None:
        rows = await self._session.execute(
            select(QuestionResponse.marks_obtained, Question.max_marks)
            .join(Question, QuestionResponse.question_id == Question.id)
            .where(QuestionResponse.student_id == student_id)
        )
        obt = 0.0
        mx = 0.0
        for r in rows.all():
            obt += float(r.marks_obtained)
            mx += float(r.max_marks)
        if mx <= 0:
            return None
        return round(obt / mx * 100.0, 1)
