from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.analytics import AnalyticsAgent
from backend.agents.context import ContextAgent
from backend.agents.formatter import FormatterAgent
from backend.agents.insights import InsightsAgent
from backend.agents.prompt import PromptAgent
from backend.agents.rag_nodes import (
    GuardrailNode,
    IntentNode,
    RetrievalNode,
    SessionMemoryNode,
)
from backend.agents.state import MentorState
from backend.agents.validation import ValidationAgent
from backend.core.config import get_settings


# ── routing predicates ────────────────────────────────────


def _route_after_guardrail(state: MentorState) -> str:
    """Block → straight to formatter; allow → continue to retrieval."""
    return "allow" if state.get("guardrail_allowed") else "block"


def _route_after_retrieval(state: MentorState) -> str:
    """Skip the structured ContextAgent for knowledge-only intents."""
    return "structured" if state.get("requires_structured", True) else "prompt"


def _route_validation(state: MentorState) -> str:
    max_retries = get_settings().pipeline_max_validation_retries
    if not state.get("validation_passed") and state.get("retry_count", 0) < max_retries:
        return "retry"
    return "format"


def _build_graph(session: AsyncSession):  # type: ignore[return]
    # nodes
    session_mem = SessionMemoryNode(session)
    intent = IntentNode()
    guardrail = GuardrailNode(session)
    retrieval = RetrievalNode(session)
    ctx = ContextAgent(session)
    analytics = AnalyticsAgent()
    prompt = PromptAgent()
    insights = InsightsAgent()
    validator = ValidationAgent()
    formatter = FormatterAgent()

    g = StateGraph(MentorState)
    g.add_node("session_memory", session_mem.run)
    g.add_node("intent_classifier", intent.run)
    g.add_node("guardrail", guardrail.run)
    g.add_node("retrieval", retrieval.run)
    g.add_node("context", ctx.run)
    g.add_node("analytics", analytics.run)
    g.add_node("prompt_builder", prompt.run)
    g.add_node("insights", insights.run)
    g.add_node("validation", validator.run)
    g.add_node("formatter", formatter.run)

    # edges — the Intent Fence is enforced *before* any data fetch.
    g.add_edge(START, "session_memory")
    g.add_edge("session_memory", "intent_classifier")
    g.add_edge("intent_classifier", "guardrail")
    g.add_conditional_edges(
        "guardrail",
        _route_after_guardrail,
        {"allow": "retrieval", "block": "formatter"},
    )
    g.add_conditional_edges(
        "retrieval",
        _route_after_retrieval,
        {"structured": "context", "prompt": "prompt_builder"},
    )
    g.add_edge("context", "analytics")
    g.add_edge("analytics", "prompt_builder")
    g.add_edge("prompt_builder", "insights")
    g.add_edge("insights", "validation")
    g.add_conditional_edges(
        "validation",
        _route_validation,
        {"retry": "insights", "format": "formatter"},
    )
    g.add_edge("formatter", END)

    return g.compile()


_DEFAULTS: MentorState = {
    "role": "",
    "user_id": "",
    "user_email": "",
    "tenant_id": "",
    "question": "",
    "student_id": None,
    "classroom_id": None,
    "conversation_id": "",
    "memory_block": "",
    "intent": "",
    "intent_confidence": 0.0,
    "intent_rule": "",
    "guardrail_allowed": False,
    "guardrail_reason": "",
    "refusal_message": "",
    "requires_rag": False,
    "requires_structured": True,
    "rag_chunks": [],
    "kg_block": "",
    "students": [],
    "raw_scores": [],
    "raw_attendance": [],
    "analytics": {},
    "system_prompt": "",
    "user_prompt": "",
    "llm_response": "",
    "validation_passed": False,
    "retry_count": 0,
    "final_response": "",
}


async def run_pipeline(
    session: AsyncSession, initial: dict[str, Any]
) -> MentorState:
    graph = _build_graph(session)
    state: MentorState = {**_DEFAULTS, **initial}  # type: ignore[misc]
    result: MentorState = await graph.ainvoke(state)  # type: ignore[assignment]
    return result
