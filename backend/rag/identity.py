from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Classroom,
    ParentStudent,
    Student,
    TeacherSubjectAssignment,
    User,
    UserRole,
)


@dataclass(frozen=True)
class IdentityClaims:
    """Single source of truth for caller scope.

    Built once at the API boundary by `IdentityClaims.from_user(...)` and
    passed by value into every layer that needs to filter data. The whole
    point is to avoid each agent re-deriving "what is this user allowed to
    see" — that derivation only happens here, and it's the input every
    filter reads from.

    Fields:
      role            — caller's role (drives the policy matrix)
      user_id         — caller's User.id
      tenant_id       — caller's school; None only for superadmin (which
                        is never served by the chat pipeline)
      student_ids     — full list of student IDs the caller may see:
                          * student → own id
                          * parent  → all linked children
                          * teacher / school_admin → empty (means "no
                            per-student narrowing"; the tenant_id +
                            classroom_ids do the scoping instead)
      classroom_ids   — classrooms the caller may see:
                          * teacher → home grade + any subject-taught class
                          * school_admin → empty (means whole tenant)
                          * student/parent → derived from their student rows
      role_visibility — which `document_chunks.role_visibility` tags this
                        caller may read. The retriever ANDs this with the
                        chunk's tag array via `&&` (PostgreSQL array
                        overlap operator).
    """

    role: UserRole
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    student_ids: tuple[uuid.UUID, ...] = ()
    classroom_ids: tuple[uuid.UUID, ...] = ()
    role_visibility: tuple[str, ...] = ()

    @classmethod
    async def from_user(cls, user: User, session: AsyncSession) -> "IdentityClaims":
        if user.tenant_id is None:
            # Superadmin can't use the chat pipeline (the chat router never
            # routes them here, but defend in depth anyway).
            raise ValueError("superadmin has no tenant scope")

        role = user.role
        tid = user.tenant_id

        student_ids: tuple[uuid.UUID, ...] = ()
        classroom_ids: tuple[uuid.UUID, ...] = ()

        if role is UserRole.student:
            sid = getattr(user, "_student_id", None)
            if sid is None:
                # Fallback resolve — defensive, get_current_user usually sets it.
                sid = await session.scalar(
                    select(Student.id).where(Student.user_id == user.id)
                )
            if sid is not None:
                student_ids = (sid,)
                cls_id = await session.scalar(
                    select(Student.classroom_id).where(Student.id == sid)
                )
                if cls_id is not None:
                    classroom_ids = (cls_id,)

        elif role is UserRole.parent:
            rows = await session.execute(
                select(ParentStudent.student_id, Student.classroom_id)
                .join(Student, Student.id == ParentStudent.student_id)
                .where(
                    ParentStudent.parent_id == user.id,
                    Student.tenant_id == tid,
                )
            )
            sids: list[uuid.UUID] = []
            cids: list[uuid.UUID] = []
            for sid_row, cid_row in rows.all():
                sids.append(sid_row)
                if cid_row is not None:
                    cids.append(cid_row)
            student_ids = tuple(sids)
            classroom_ids = tuple(dict.fromkeys(cids))  # dedupe preserving order

        elif role is UserRole.teacher:
            home = await session.execute(
                select(Classroom.id).where(
                    Classroom.tenant_id == tid, Classroom.teacher_id == user.id
                )
            )
            taught = await session.execute(
                select(TeacherSubjectAssignment.classroom_id).where(
                    TeacherSubjectAssignment.tenant_id == tid,
                    TeacherSubjectAssignment.teacher_id == user.id,
                )
            )
            cids = [*home.scalars().all(), *taught.scalars().all()]
            classroom_ids = tuple(dict.fromkeys(cids))

        elif role is UserRole.school_admin:
            # No narrowing — tenant_id is the boundary.
            pass

        return cls(
            role=role,
            user_id=user.id,
            tenant_id=tid,
            student_ids=student_ids,
            classroom_ids=classroom_ids,
            role_visibility=_visibility_tags_for(role),
        )

    # ── convenience predicates ─────────────────────────────────

    def may_see_student(self, student_id: uuid.UUID) -> bool:
        """Per-student visibility check.

        Teachers and school admins do not narrow by student id — they're
        bounded by tenant_id and (for teachers) classroom_ids. Student and
        parent roles, however, are strictly bounded by `student_ids`.
        """
        if self.role in (UserRole.teacher, UserRole.school_admin):
            return True
        return student_id in self.student_ids

    def may_see_classroom(self, classroom_id: uuid.UUID) -> bool:
        if self.role is UserRole.school_admin:
            return True
        return classroom_id in self.classroom_ids


def _visibility_tags_for(role: UserRole) -> tuple[str, ...]:
    """Which `document_chunks.role_visibility` tags this role may read.

    Tags on chunks are at-most-one of: "public" (all roles), "student",
    "parent", "teacher", "school_admin". A chunk is visible to the caller
    iff their tag set overlaps with the chunk's tag array.
    """
    base = ("public",)
    if role is UserRole.student:
        return (*base, "student")
    if role is UserRole.parent:
        return (*base, "parent")
    if role is UserRole.teacher:
        return (*base, "teacher", "student", "parent")  # teacher sees parent/student facing too
    if role is UserRole.school_admin:
        return (*base, "school_admin", "teacher", "student", "parent")
    return base
