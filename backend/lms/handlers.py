"""Event handlers — one per LMS event.

These are the integration shims between FastAPI and the (still-empty)
``backend/agents`` and ``backend/pipelines`` packages. Each handler:

  1. Receives a typed payload from the router.
  2. Calls into a pipeline (placeholder for now — a deterministic stub that
     produces a believable insight, so the round-trip can be tested
     end-to-end before LangGraph code lands).
  3. Posts results back to Frappe via :class:`backend.lms.client.LMSCallbackClient`.

When the real LangGraph pipelines arrive, only the inner ``_pipeline_*``
helpers in this module change. The wire format and Frappe-side surface
stay stable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.core.logging import get_logger
from backend.lms.client import get_client
from backend.lms.models import (
    AssignmentGradedPayload,
    ConversationMessageOut,
    DailyRiskRefreshPayload,
    EnrolledPayload,
    InsightOut,
    LessonChatMessagePayload,
    LessonCompletedPayload,
    PostQuizRequestPayload,
    QuizSubmittedPayload,
    RiskScoreOut,
    WeeklySummariesPayload,
)

logger = get_logger(__name__)

PIPELINE_VERSION = "stub-0.1"
MODEL_LABEL = "stub"


# ---------------------------------------------------------------------------
# Quiz submitted → post-quiz feedback insight
# ---------------------------------------------------------------------------
async def handle_quiz_submitted(payload: QuizSubmittedPayload) -> dict[str, Any]:
    insight = _pipeline_post_quiz(payload)
    result = await get_client().ingest_insights([insight])
    logger.info(
        "lms.event.quiz_submitted.handled",
        member=payload.member,
        submission=payload.submission_id,
        score=payload.score,
        result=result,
    )
    return {"ok": True, "kind": insight.kind}


def _pipeline_post_quiz(payload: QuizSubmittedPayload) -> InsightOut:
    total = len(payload.results) or 1
    correct = sum(1 for r in payload.results if r.is_correct)
    accuracy = correct / total
    bullets = [
        f"You answered {correct}/{total} correctly ({accuracy:.0%}).",
    ]
    if payload.passed:
        bullets.append("You cleared the passing bar — strong work.")
    else:
        bullets.append("Below the passing bar — review wrong answers and re-try.")
    if any(r.is_correct == 0 for r in payload.results):
        wrong = [r.question for r in payload.results if not r.is_correct and r.question]
        bullets.append(f"Focus on these questions: {', '.join(wrong[:5])}.")

    return InsightOut(
        member=payload.member,
        kind="post_quiz_feedback",
        title=f"Quiz feedback — {payload.percentage or 0:.0f}%",
        summary=" ".join(bullets),
        payload={
            "submission_id": payload.submission_id,
            "accuracy": accuracy,
            "passed": payload.passed,
            "wrong_questions": [r.question for r in payload.results if not r.is_correct],
        },
        context_doctype="LMS Quiz Submission",
        context_name=payload.submission_id,
        course=payload.course,
        model=MODEL_LABEL,
        version=PIPELINE_VERSION,
    )


# ---------------------------------------------------------------------------
# Assignment graded → grade-assist insight (drafted feedback for student)
# ---------------------------------------------------------------------------
async def handle_assignment_graded(payload: AssignmentGradedPayload) -> dict[str, Any]:
    insight = InsightOut(
        member=payload.member,
        kind="grade_assist",
        title=f"Assignment graded — {payload.status}",
        summary=(payload.comments or "")[:500] or f"Your assignment was marked {payload.status}.",
        payload={"submission_id": payload.submission_id, "status": payload.status},
        context_doctype="LMS Assignment Submission",
        context_name=payload.submission_id,
        course=payload.course,
        model=MODEL_LABEL,
        version=PIPELINE_VERSION,
    )
    await get_client().ingest_insights([insight])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Lesson completed → mastery refresh (stub: log only)
# ---------------------------------------------------------------------------
async def handle_lesson_completed(payload: LessonCompletedPayload) -> dict[str, Any]:
    logger.info(
        "lms.event.lesson_completed.handled",
        member=payload.member,
        course=payload.course,
        lesson=payload.lesson,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Enrollment → baseline diagnostic suggestion
# ---------------------------------------------------------------------------
async def handle_enrolled(payload: EnrolledPayload) -> dict[str, Any]:
    insight = InsightOut(
        member=payload.member,
        kind="study_plan",
        title="Welcome — your study plan",
        summary="Take the diagnostic at the start of the course so I can tailor recommendations.",
        payload={"enrollment_id": payload.enrollment_id},
        context_doctype="LMS Enrollment",
        context_name=payload.enrollment_id,
        course=payload.course,
        model=MODEL_LABEL,
        version=PIPELINE_VERSION,
    )
    await get_client().ingest_insights([insight])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Explicit re-request (called from the SPA when the student clicks
# "analyze my quiz again" or arrives on a quiz page that has no insight yet)
# ---------------------------------------------------------------------------
async def handle_post_quiz_request(payload: PostQuizRequestPayload) -> dict[str, Any]:
    logger.info("lms.event.post_quiz_request", submission=payload.submission_id)
    return {"ok": True, "noop": True}


# ---------------------------------------------------------------------------
# Lesson chat — student typed a message; we generate an assistant reply.
# Stub returns a canned acknowledgement; real impl will RAG over lesson body.
# ---------------------------------------------------------------------------
async def handle_lesson_chat_message(payload: LessonChatMessagePayload) -> dict[str, Any]:
    reply = ConversationMessageOut(
        conversation=payload.conversation,
        role="assistant",
        content=(
            "Got it — I'll help you with this lesson. "
            "(stub reply; real RAG-grounded answer comes once the pipeline is wired)"
        ),
        model=MODEL_LABEL,
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
    )
    await get_client().append_message(reply)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Daily risk refresh — scheduled job. Stub returns no rows.
# Real impl: read enrollments + activity from Frappe, compute risk per student.
# ---------------------------------------------------------------------------
async def handle_daily_risk_refresh(payload: DailyRiskRefreshPayload) -> dict[str, Any]:
    rows: list[RiskScoreOut] = []
    if rows:
        await get_client().ingest_risk_scores(rows)
    logger.info("lms.event.daily_risk_refresh", as_of=payload.as_of, count=len(rows))
    return {"ok": True, "count": len(rows)}


async def handle_weekly_summaries(payload: WeeklySummariesPayload) -> dict[str, Any]:
    insights: list[InsightOut] = []
    if insights:
        await get_client().ingest_insights(insights)
    logger.info("lms.event.weekly_summaries", as_of=payload.as_of, count=len(insights))
    return {"ok": True, "count": len(insights)}


# ---------------------------------------------------------------------------
# Dispatch table — keyed by the URL slug Frappe POSTs to.
# ---------------------------------------------------------------------------
EVENT_DISPATCH: dict[str, Any] = {
    "quiz_submitted": (QuizSubmittedPayload, handle_quiz_submitted),
    "assignment_graded": (AssignmentGradedPayload, handle_assignment_graded),
    "lesson_completed": (LessonCompletedPayload, handle_lesson_completed),
    "enrolled": (EnrolledPayload, handle_enrolled),
    "post_quiz_request": (PostQuizRequestPayload, handle_post_quiz_request),
    "lesson_chat_message": (LessonChatMessagePayload, handle_lesson_chat_message),
    "daily_risk_refresh": (DailyRiskRefreshPayload, handle_daily_risk_refresh),
    "weekly_summaries": (WeeklySummariesPayload, handle_weekly_summaries),
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
