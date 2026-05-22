from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.state import MentorState
from backend.core.logging import get_logger
from backend.db.models import UserRole
from backend.rag.guardrail import Guardrail
from backend.rag.identity import IdentityClaims
from backend.rag.intent import Intent, IntentClassifier
from backend.rag.intent_llm import LLMIntentClassifier
from backend.rag.knowledge_graph import KnowledgeGraph
from backend.rag.retriever import Retriever
from backend.rag.session import SessionManager

logger = get_logger(__name__)


# ── Session memory ─────────────────────────────────────────


class SessionMemoryNode:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, state: MentorState) -> dict[str, Any]:
        mgr = SessionManager(self._session)
        mem = await mgr.load(state.get("conversation_id"))
        return {"memory_block": mem.to_prompt_block()}


# ── Intent classification ──────────────────────────────────


class IntentNode:
    """Rule-based classifier with optional LLM fallback for ambiguous cases.

    The rule classifier is fast and deterministic. When its confidence
    drops below `LLMIntentClassifier.LOW_CONFIDENCE_THRESHOLD` we consult
    qwen2.5 with a constrained JSON schema. Adversarial intents
    (injection / pii / cross-student) at high confidence are NEVER
    overridden by the LLM — that's defensive: the rules are how we
    enforce safety, the LLM is for nuance.
    """

    def __init__(self) -> None:
        self._rules = IntentClassifier()
        self._llm = LLMIntentClassifier()

    async def run(self, state: MentorState) -> dict[str, Any]:
        q = state.get("question", "")
        role = state.get("role", "")
        rules_cls = self._rules.classify(q, role)
        final = await self._llm.classify(q, role, rules=rules_cls)
        return {
            "intent": final.intent.value,
            "intent_confidence": final.confidence,
            "intent_rule": final.matched_rule,
        }


# ── Guardrail / Intent Fence ───────────────────────────────


class GuardrailNode:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._gr = Guardrail()

    async def run(self, state: MentorState) -> dict[str, Any]:
        # Re-derive identity here (cheap — small queries). We don't try to
        # serialise it through state because it carries DB objects.
        identity = await _identity_from_state(self._session, state)

        # Recreate a Classification stub from state (Intent + rule).
        intent = _intent_from_state(state)

        decision = self._gr.evaluate(
            _stub_classification(intent, state),
            identity,
        )

        out: dict[str, Any] = {
            "guardrail_allowed": decision.allowed,
            "guardrail_reason": decision.reason,
            "refusal_message": decision.refusal_message or "",
            "requires_rag": decision.requires_rag,
            "requires_structured": decision.requires_structured,
        }
        if not decision.allowed:
            logger.info(
                "guardrail.blocked",
                role=state["role"], intent=intent.value,
                reason=decision.reason,
            )
        return out


# ── Retrieval (vector + KG) ────────────────────────────────


class RetrievalNode:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, state: MentorState) -> dict[str, Any]:
        out: dict[str, Any] = {"rag_chunks": [], "kg_block": ""}

        if not state.get("guardrail_allowed"):
            return out

        identity = await _identity_from_state(self._session, state)
        intent = _intent_from_state(state)

        # Vector retrieval — only if intent needs RAG.
        if state.get("requires_rag"):
            retriever = Retriever(self._session)
            chunks = await retriever.retrieve(
                state.get("question", ""), identity, intent=intent
            )
            out["rag_chunks"] = [
                {
                    "id": c.chunk_id,
                    "doc_id": c.document_id,
                    "title": c.title,
                    "kind": c.kind,
                    "content": c.content,
                    "score": c.score,
                    "subject": c.subject,
                    "topic": c.topic,
                }
                for c in chunks
            ]

        # Knowledge graph block — only for student-focused intents and
        # where we have a concrete student to centre on. This keeps the
        # KG block out of broad class-wide queries (where it'd be noise).
        target_sid = _resolve_target_student_id(state, identity)
        if target_sid is not None and intent in {
            Intent.own_performance,
            Intent.child_performance,
            Intent.attendance_query,
            Intent.proactive_suggestion,
        }:
            try:
                kg = KnowledgeGraph(self._session, identity)
                nb = await kg.neighbourhood(student_id=target_sid, hops=2)
                out["kg_block"] = _render_neighbourhood(nb)
            except PermissionError:
                # Defence in depth — guardrail should have caught this,
                # but if it didn't we just skip the block silently.
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("kg.failed", error=str(exc))

        return out


# ── helpers ────────────────────────────────────────────────


async def _identity_from_state(
    session: AsyncSession, state: MentorState
) -> IdentityClaims:
    # We rebuild a minimal User stub for `IdentityClaims.from_user`. The
    # alternative is to plumb the User object through state, which means
    # serialising an ORM object through LangGraph — fragile. The DB hits
    # in IdentityClaims.from_user are tiny (one or two indexed queries).
    from backend.db.models import User

    user = await session.get(User, uuid.UUID(state["user_id"]))
    if user is None:
        raise RuntimeError("user disappeared mid-pipeline")

    # Resolve student backing id for student users so identity is complete.
    if user.role is UserRole.student:
        from sqlalchemy import select
        from backend.db.models import Student
        sid = await session.scalar(
            select(Student.id).where(Student.user_id == user.id)
        )
        if sid is not None:
            user._student_id = sid  # type: ignore[attr-defined]

    return await IdentityClaims.from_user(user, session)


def _intent_from_state(state: MentorState) -> Intent:
    raw = state.get("intent") or Intent.off_topic.value
    try:
        return Intent(raw)
    except ValueError:
        return Intent.off_topic


def _stub_classification(intent: Intent, state: MentorState):
    # Pass-through wrapper to satisfy Guardrail.evaluate which expects
    # Classification — but we already classified upstream. This keeps
    # Guardrail and Classifier loosely coupled.
    from backend.rag.intent import Classification
    return Classification(
        intent=intent,
        confidence=float(state.get("intent_confidence", 0.0)),
        matched_rule=state.get("intent_rule", "unknown"),
    )


def _resolve_target_student_id(
    state: MentorState, identity: IdentityClaims
) -> uuid.UUID | None:
    raw = state.get("student_id")
    if raw:
        try:
            sid = uuid.UUID(raw)
        except ValueError:
            return None
        return sid if identity.may_see_student(sid) else None
    # Default to the caller's single student if there is one (student or
    # single-child parent).
    if len(identity.student_ids) == 1:
        return identity.student_ids[0]
    return None


def _render_neighbourhood(nb) -> str:
    lines = [f"KNOWLEDGE GRAPH (centre = {nb.center.label}):"]
    for e in nb.edges:
        if e.rel == "weak_in":
            pct = e.dst.extra.get("accuracy_pct", "?")
            lines.append(f"  • weak in {e.dst.label} ({pct}%)")
        elif e.rel == "enrolled_in":
            lines.append(f"  • class: {e.dst.label}")
        elif e.rel == "taught_by":
            sub = e.dst.extra.get("subject", "")
            sub_label = f" ({sub})" if sub else ""
            lines.append(f"  • teacher: {e.dst.label}{sub_label}")
        elif e.rel == "has_classmate":
            lines.append(f"  • classmate: {e.dst.label}")
        elif e.rel == "has_parent":
            lines.append(f"  • parent: {e.dst.label}")
    return "\n".join(lines) if len(lines) > 1 else ""
