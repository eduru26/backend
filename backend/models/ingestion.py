from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.db.models import (
    AssessmentType,
    AttendanceStatus,
    QuestionDifficulty,
)


# ─── /ingest/students ────────────────────────────────────


class StudentRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=200)
    grade: int = Field(ge=1, le=12)
    section: str = Field(min_length=1, max_length=8)
    parent_email: EmailStr | None = None


# ─── /ingest/attendance ──────────────────────────────────


class AttendanceRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    student_handle: str = Field(min_length=1, max_length=64)
    status: AttendanceStatus


# ─── /ingest/question-bank ───────────────────────────────


class AssessmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AssessmentType
    title: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=100)
    assessment_date: date
    max_marks: Decimal = Field(gt=0, max_digits=8, decimal_places=2)


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_no: int = Field(ge=1, le=10000)
    topic: str = Field(min_length=1, max_length=150)
    max_marks: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    difficulty: QuestionDifficulty


class QuestionBankIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment: AssessmentIn
    questions: list[QuestionIn] = Field(min_length=1)

    @field_validator("questions")
    @classmethod
    def unique_question_nos(cls, v: list[QuestionIn]) -> list[QuestionIn]:
        seen: set[int] = set()
        for q in v:
            if q.question_no in seen:
                raise ValueError(f"duplicate question_no: {q.question_no}")
            seen.add(q.question_no)
        return v


class QuestionBankOut(BaseModel):
    assessment_id: uuid.UUID
    questions_created: int
    upload_id: uuid.UUID


# ─── /ingest/marks ───────────────────────────────────────


class MarksRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_id: uuid.UUID
    student_handle: str = Field(min_length=1, max_length=64)
    question_no: int = Field(ge=1, le=10000)
    marks_obtained: Decimal = Field(ge=0, max_digits=8, decimal_places=2)


# ─── Common response ─────────────────────────────────────


class IngestResult(BaseModel):
    upload_id: uuid.UUID
    rows_processed: int
    duplicate: bool = False
    detail: str | None = None
