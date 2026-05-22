"""Pydantic schemas matching the wire format emitted by Frappe's
``ai_mentor.mentor.events`` and ``ai_mentor.mentor.bridge``.

The envelope shape (``BridgeEnvelope``) is shared across all events; the
``payload`` is one of the per-event payload classes. We accept extra keys
(``extra="allow"``) so a future Frappe-side field doesn't 4xx us — the
backend can ignore unknowns and process what it understands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class BridgeEnvelope(_Loose, Generic[T]):
    event: str
    site: str | None = None
    ts: datetime | None = None
    payload: T


# ---------------------------------------------------------------------------
# Per-event payloads (mirror ai_mentor.mentor.events)
# ---------------------------------------------------------------------------


class QuizResultRow(_Loose):
    question: str | None = None  # LMS Question name
    is_correct: int = 0
    marks: float | None = None
    marks_out_of: float | None = None
    answer: str | None = None


class QuizSubmittedPayload(_Loose):
    submission_id: str
    quiz: str | None = None
    course: str | None = None
    member: str
    score: float | None = None
    score_out_of: float | None = None
    percentage: float | None = None
    passing_percentage: float | None = None
    passed: bool = False
    results: list[QuizResultRow] = Field(default_factory=list)


class AssignmentGradedPayload(_Loose):
    submission_id: str
    assignment: str
    course: str | None = None
    member: str
    status: Literal["Pass", "Fail"]
    evaluator: str | None = None
    comments: str | None = None
    answer: str | None = None
    type: str | None = None


class LessonCompletedPayload(_Loose):
    progress_id: str
    course: str | None = None
    chapter: str | None = None
    lesson: str | None = None
    member: str
    completed_at: datetime | None = None


class EnrolledPayload(_Loose):
    enrollment_id: str
    course: str | None = None
    member: str
    from_batch: str | None = None


class PostQuizRequestPayload(_Loose):
    submission_id: str


class LessonChatMessagePayload(_Loose):
    conversation: str
    message: str  # AI Conversation Message name
    lesson: str | None = None
    course: str | None = None
    content: str


class DailyRiskRefreshPayload(_Loose):
    as_of: datetime | str | None = None


class WeeklySummariesPayload(_Loose):
    as_of: datetime | str | None = None


# ---------------------------------------------------------------------------
# Outbound (callback) shapes — what we POST back to Frappe webhook.ingest_*
# ---------------------------------------------------------------------------


class InsightOut(_Loose):
    member: str
    kind: Literal[
        "post_quiz_feedback",
        "weak_topics",
        "risk_score",
        "study_plan",
        "weekly_summary",
        "grade_assist",
        "lesson_chat",
    ]
    title: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    context_doctype: str | None = None
    context_name: str | None = None
    course: str | None = None
    lesson: str | None = None
    model: str | None = None
    version: str | None = None


class QuestionTagOut(_Loose):
    question: str
    topic: str
    subtopic: str | None = None
    skill: str | None = None
    difficulty: float | None = None
    confidence: float | None = None
    source: Literal["ai", "teacher", "import"] = "ai"


class RiskScoreOut(_Loose):
    member: str
    course: str | None = None
    score: float
    level: Literal["low", "medium", "high"]
    model: str | None = None
    attendance_pct: float | None = None
    avg_quiz_pct: float | None = None
    lessons_completed_pct: float | None = None
    days_since_last_activity: int | None = None
    video_skim_ratio: float | None = None
    factors_json: dict[str, Any] | None = None


class ConversationMessageOut(_Loose):
    conversation: str
    role: Literal["assistant", "system", "tool"]
    content: str
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    citations: list[dict[str, Any]] | None = None
