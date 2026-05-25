"""School-admin question-paper template endpoints.

A template is the skeleton of an exam: ordered sections ("mains"), each with
a question type, a count, marks-per-question, and an optional "answer N of M"
setting. Teachers pick a template when generating a paper.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.db.models import QuestionPaperTemplate, User, UserRole
from backend.db.repositories.question_papers import TemplateRepository

router = APIRouter(prefix="/school/templates", tags=["qp-templates"])
SchoolAdminDep = Annotated[User, Depends(require_roles(UserRole.school_admin))]

# Granular question types — kept in sync with paper_generator._SYS_BY_TYPE
# (per-type system prompt) and the frontend's TYPE_LABEL dict. New
# values must be added in all three places at once.
#   - mcq           Multiple choice (4 options, mark the correct one)
#   - fill_blank    Fill in the blank
#   - true_false    True / False
#   - one_word      Single-word or short-phrase answer
#   - short_answer  2-3 sentence response
#   - long_answer   Detailed paragraph / essay
#   - objective     legacy umbrella for mcq/fill_blank/true_false/one_word;
#                   accepted on read so existing templates still load, but
#                   not surfaced in the admin create UI.
QuestionType = Literal[
    "mcq",
    "fill_blank",
    "true_false",
    "one_word",
    "short_answer",
    "long_answer",
    "objective",
]
ExamType = Literal["unit_test", "semester_exam"]


# ── Schemas ──────────────────────────────────────────────


class SectionIn(BaseModel):
    section_no: int
    title: str
    question_type: QuestionType
    question_count: int           # questions a student must ANSWER
    marks_per_question: float
    optional_of: int | None = None  # if set: choose question_count of optional_of

    @field_validator("question_count")
    @classmethod
    def _count_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("question_count must be at least 1")
        return v

    @field_validator("marks_per_question")
    @classmethod
    def _marks_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("marks_per_question must be positive")
        return v

    @model_validator(mode="after")
    def _optional_valid(self) -> "SectionIn":
        if self.optional_of is not None and self.optional_of < self.question_count:
            raise ValueError("optional_of must be >= question_count")
        return self

    def section_marks(self) -> float:
        return self.question_count * self.marks_per_question


class TemplateIn(BaseModel):
    name: str
    exam_type: ExamType
    total_marks: float
    sections: list[SectionIn]
    # Allotted time for the paper in minutes. Optional — old templates
    # may not have it set. Bounded loosely (5 min – 8 h) so an
    # accidental input like 600 hours doesn't slip past.
    duration_minutes: int | None = None

    @model_validator(mode="after")
    def _marks_add_up(self) -> "TemplateIn":
        if not self.sections:
            raise ValueError("a template needs at least one section")
        computed = sum(s.section_marks() for s in self.sections)
        # tolerate float noise
        if abs(computed - self.total_marks) > 0.01:
            raise ValueError(
                f"section marks total {computed:g} but template total_marks is "
                f"{self.total_marks:g}"
            )
        return self

    @field_validator("duration_minutes")
    @classmethod
    def _duration_range(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 5 or v > 8 * 60:
            raise ValueError("duration_minutes must be between 5 and 480")
        return v


class TemplateOut(BaseModel):
    id: str
    name: str
    exam_type: str
    total_marks: float
    sections: list
    is_active: bool
    duration_minutes: int | None = None


def _out(t: QuestionPaperTemplate) -> TemplateOut:
    return TemplateOut(
        id=str(t.id), name=t.name, exam_type=t.exam_type,
        total_marks=float(t.total_marks), sections=t.sections or [],
        is_active=t.is_active, duration_minutes=t.duration_minutes,
    )


# ── Endpoints ────────────────────────────────────────────


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    user: SchoolAdminDep, session: SessionDep, exam_type: str | None = None
) -> list[TemplateOut]:
    tid = assert_tenant(user)
    rows = await TemplateRepository(session).list_templates(tid, exam_type=exam_type)
    return [_out(t) for t in rows]


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    body: TemplateIn, user: SchoolAdminDep, session: SessionDep
) -> TemplateOut:
    tid = assert_tenant(user)
    repo = TemplateRepository(session)
    tpl = await repo.create(
        tenant_id=tid, name=body.name, exam_type=body.exam_type,
        total_marks=Decimal(str(body.total_marks)),
        sections=[s.model_dump() for s in body.sections],
        created_by=user.id,
        duration_minutes=body.duration_minutes,
    )
    return _out(tpl)


@router.patch("/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: str, body: TemplateIn, user: SchoolAdminDep, session: SessionDep
) -> TemplateOut:
    tid = assert_tenant(user)
    repo = TemplateRepository(session)
    try:
        tpl = await repo.get(uuid.UUID(template_id), tid)
    except ValueError:
        raise HTTPException(400, "invalid template id")
    if tpl is None:
        raise HTTPException(404, "template not found")
    await repo.update(
        tpl, name=body.name, total_marks=Decimal(str(body.total_marks)),
        sections=[s.model_dump() for s in body.sections],
        duration_minutes=body.duration_minutes,
        duration_minutes_explicit=True,
    )
    return _out(tpl)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str, user: SchoolAdminDep, session: SessionDep
) -> None:
    tid = assert_tenant(user)
    repo = TemplateRepository(session)
    try:
        tpl = await repo.get(uuid.UUID(template_id), tid)
    except ValueError:
        raise HTTPException(400, "invalid template id")
    if tpl is None:
        raise HTTPException(404, "template not found")
    if await repo.is_referenced(tpl.id):
        raise HTTPException(
            409, "this template has been used to generate a paper and can't be deleted"
        )
    await repo.delete(tpl)
