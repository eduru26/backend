from __future__ import annotations

from dataclasses import dataclass

from backend.db.models import UserRole
from backend.rag.identity import IdentityClaims
from backend.rag.intent import Classification, Intent


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    intent: Intent
    reason: str                # short audit reason
    refusal_message: str | None = None  # user-facing refusal if blocked
    requires_rag: bool = False          # tell pipeline to run retriever
    requires_structured: bool = True    # tell pipeline to run ContextAgent


# Static policy matrix.
#
# For each role, the set of intents that are explicitly allowed. Intents
# absent from a role's set are denied — fail closed. This is the
# single, auditable source of truth for "who can ask what".
_ALLOW: dict[UserRole, set[Intent]] = {
    UserRole.student: {
        Intent.own_performance,
        Intent.attendance_query,
        Intent.curriculum_qa,
        Intent.policy_qa,
        Intent.announcement_recall,
        Intent.proactive_suggestion,
    },
    UserRole.parent: {
        Intent.child_performance,
        Intent.attendance_query,
        Intent.curriculum_qa,
        Intent.policy_qa,
        Intent.announcement_recall,
        Intent.proactive_suggestion,
    },
    UserRole.teacher: {
        Intent.own_performance,            # used when teacher focuses on a student
        Intent.child_performance,          # alias for teacher-on-student deep dive
        Intent.class_aggregate,
        Intent.attendance_query,
        Intent.curriculum_qa,
        Intent.policy_qa,
        Intent.announcement_recall,
        Intent.proactive_suggestion,
    },
    UserRole.school_admin: {
        Intent.school_aggregate,
        Intent.class_aggregate,
        Intent.curriculum_qa,
        Intent.policy_qa,
        Intent.announcement_recall,
        Intent.proactive_suggestion,
    },
}

# Intents that need the vector retriever to fire.
_RAG_INTENTS: set[Intent] = {
    Intent.curriculum_qa,
    Intent.policy_qa,
    Intent.announcement_recall,
}

# Intents where the structured (DB aggregates) path is unnecessary.
_KNOWLEDGE_ONLY: set[Intent] = {
    Intent.policy_qa,
    Intent.curriculum_qa,
}


_REFUSAL_PII = (
    "I can't share personal contact details or identification information. "
    "If you need that, please ask the school office directly."
)

_REFUSAL_CROSS_STUDENT = (
    "I can only share information about your own academic record. "
    "For details about another student, please reach out to their family or the teacher."
)

_REFUSAL_INJECTION = (
    "I can't follow that instruction. I'm an academic mentor and I'll stick to "
    "questions about learning, performance, and school topics."
)

_REFUSAL_OFF_TOPIC = (
    "That's outside what I can help with. I'm an academic mentor — try asking "
    "about your studies, performance, or attendance instead."
)

_REFUSAL_NOT_ALLOWED = (
    "That request isn't available for your account. If you think this is a "
    "mistake, please contact your school administrator."
)


class Guardrail:
    """Intent Fence — applies the (role, intent) policy matrix.

    Lives between the IntentClassifier and the retrieval / context layer.
    Once a request crosses this boundary, downstream layers can assume the
    caller is authorised for the intent and just have to enforce data
    scope (which IdentityClaims provides).
    """

    def evaluate(
        self, classification: Classification, identity: IdentityClaims
    ) -> GuardrailDecision:
        intent = classification.intent

        # 1. Hard blocks — same for every role.
        if intent is Intent.prompt_injection:
            return GuardrailDecision(
                False, intent, classification.matched_rule,
                refusal_message=_REFUSAL_INJECTION,
            )
        if intent is Intent.pii_request:
            return GuardrailDecision(
                False, intent, classification.matched_rule,
                refusal_message=_REFUSAL_PII,
            )
        if intent is Intent.cross_student_query:
            return GuardrailDecision(
                False, intent, classification.matched_rule,
                refusal_message=_REFUSAL_CROSS_STUDENT,
            )
        if intent is Intent.off_topic:
            return GuardrailDecision(
                False, intent, classification.matched_rule,
                refusal_message=_REFUSAL_OFF_TOPIC,
            )

        # 2. Role x intent allow-list. Fail closed.
        allowed_intents = _ALLOW.get(identity.role, set())
        if intent not in allowed_intents:
            return GuardrailDecision(
                False, intent, f"role={identity.role.value}:intent_not_allowed",
                refusal_message=_REFUSAL_NOT_ALLOWED,
            )

        # 3. Allowed — decide which retrieval layers to fire.
        return GuardrailDecision(
            True, intent, f"role={identity.role.value}:allowed",
            requires_rag=intent in _RAG_INTENTS,
            requires_structured=intent not in _KNOWLEDGE_ONLY,
        )
