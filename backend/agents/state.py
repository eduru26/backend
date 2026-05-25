from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class MentorState(TypedDict):
    # ── input ──────────────────────────────────────────────
    role: str            # "student" | "parent" | "teacher" | "school_admin"
    user_id: str
    user_email: str
    tenant_id: str       # school UUID — all DB queries are scoped to this
    question: str
    student_id: str | None   # target student UUID string; None = whole class (teacher)
    # Teacher-only: when student_id is None, restrict "whole class" context to
    # one specific classroom instead of every student in the tenant. Ignored
    # for student/parent roles (their scope is fixed by who they are).
    classroom_id: str | None
    conversation_id: str

    # ── session memory (SessionManager) ─────────────────────
    # Last-N user/assistant turns rendered as a prompt block.
    memory_block: str

    # ── intent + guardrail ─────────────────────────────────
    intent: str                  # Intent enum value
    intent_confidence: float
    intent_rule: str
    guardrail_allowed: bool
    guardrail_reason: str
    refusal_message: str         # set iff guardrail blocked the turn
    requires_rag: bool
    requires_structured: bool

    # ── retrieved (Retriever) ──────────────────────────────
    rag_chunks: list[dict[str, Any]]   # [{id, doc_id, title, kind, content, score, ...}]

    # ── knowledge graph (KnowledgeGraph) ───────────────────
    kg_block: str                # rendered neighbourhood for the prompt

    # ── context (ContextAgent) ──────────────────────────────
    students: list[dict[str, Any]]        # [{id, name, grade, section}]
    raw_scores: list[dict[str, Any]]      # [{student_id, subject, topic, marks_obtained, max_marks, date}]
    raw_attendance: list[dict[str, Any]]  # [{student_id, date, status}]
    teachers: list[dict[str, Any]]        # [{id, name, specialization, home_classroom, subject_assignments}] — admin only

    # ── analytics (AnalyticsAgent) ──────────────────────────
    analytics: dict[str, Any]

    # ── prompts (PromptAgent) ───────────────────────────────
    system_prompt: str
    user_prompt: str

    # ── llm output (InsightsAgent) ──────────────────────────
    llm_response: str

    # ── validation (ValidationAgent) ────────────────────────
    validation_passed: bool
    retry_count: int

    # ── final output (FormatterAgent) ───────────────────────
    final_response: str
