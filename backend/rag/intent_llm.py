from __future__ import annotations

import json
import re
from typing import Iterable

from backend.core.logging import get_logger
from backend.llm.ollama import get_ollama
from backend.rag.intent import Classification, Intent

logger = get_logger(__name__)


# Stable list shown to the LLM. Order matters for ties — earlier wins.
_INTENT_VOCAB: tuple[Intent, ...] = (
    Intent.policy_qa,
    Intent.curriculum_qa,
    Intent.announcement_recall,
    Intent.own_performance,
    Intent.child_performance,
    Intent.class_aggregate,
    Intent.school_aggregate,
    Intent.attendance_query,
    Intent.proactive_suggestion,
    Intent.cross_student_query,
    Intent.pii_request,
    Intent.prompt_injection,
    Intent.off_topic,
)

_SYSTEM = (
    "You are an intent classifier for an academic mentor chatbot. "
    "Given a user role and their question, return a single JSON object: "
    '{"intent": "<one of the allowed intents>"}. '
    "No prose, no markdown, no code fences. Pick the BEST single intent.\n\n"
    "Critical distinctions:\n"
    "  - pii_request: ONLY when the user explicitly asks for personal contact info "
    "(phone number, home address, email address, Aadhaar, SSN, date of birth). "
    "A school_admin asking for student details, a list of students, or complete student "
    "records is school_aggregate — NOT pii_request.\n"
    "  - cross_student_query: ONLY when a student or parent asks about a different "
    "student. Never applies to teacher or school_admin roles.\n"
    "  - school_aggregate: school_admin asking about enrolled students, teachers, "
    "grades, or any school-wide data.\n"
    "  - class_aggregate: teacher or admin asking about a specific class or section.\n\n"
    "Allowed intents:\n"
    + "\n".join(f"  - {i.value}" for i in _INTENT_VOCAB)
)


_USER_TEMPLATE = (
    "Role: {role}\n"
    "Question: {question}\n\n"
    "Respond with one JSON object and nothing else."
)


class LLMIntentClassifier:
    """LLM-backed second opinion for the IntentClassifier.

    Only called when the rule-based classifier's confidence falls below
    a threshold (default 0.5). This keeps latency cheap for the common
    case (where the rules are confident) and only spends an LLM call on
    the ambiguous tail. Output is constrained to the `Intent` enum — we
    parse strict JSON and snap any unknown value back to `off_topic`.

    We pass through the existing `Classification` shape so the rest of
    the pipeline doesn't need to know whether classification came from
    rules or LLM.
    """

    LOW_CONFIDENCE_THRESHOLD = 0.5

    def should_consult(self, classification: Classification) -> bool:
        # Never override hard adversarial / safety classifications — those
        # are 1.0 confidence by design and should remain blocked.
        if classification.intent in (
            Intent.prompt_injection,
            Intent.pii_request,
            Intent.cross_student_query,
        ) and classification.confidence >= 0.9:
            return False
        return classification.confidence < self.LOW_CONFIDENCE_THRESHOLD

    async def classify(
        self, question: str, role: str, *, rules: Classification
    ) -> Classification:
        if not self.should_consult(rules):
            return rules

        try:
            verdict = await get_ollama().complete(
                _SYSTEM,
                _USER_TEMPLATE.format(role=role, question=question),
                timeout=10.0,  # short — this is a one-token-ish classification
            )
        except Exception as exc:  # noqa: BLE001 — degrade to rules
            logger.warning("intent.llm.failed", error=str(exc))
            return rules

        intent = _parse_intent(verdict, _INTENT_VOCAB)
        if intent is None:
            logger.info("intent.llm.unparseable", raw=verdict[:120])
            return rules

        # Safety invariant: the LLM is allowed to disambiguate between
        # non-adversarial intents, but it must NEVER escalate a query to an
        # adversarial intent (pii_request / prompt_injection / cross_student_query)
        # when the rule classifier didn't already flag it. The rule-based
        # classifier is the sole authoritative source for safety blocks — the
        # LLM is too prone to false-positives (e.g. classifying "give complete
        # details about students" as pii_request for a school_admin).
        _ADVERSARIAL = {Intent.pii_request, Intent.prompt_injection, Intent.cross_student_query}
        if intent in _ADVERSARIAL and rules.intent not in _ADVERSARIAL:
            logger.info(
                "intent.llm.adversarial_override_rejected",
                llm_intent=intent.value,
                rules_intent=rules.intent.value,
            )
            return rules

        # Replace the rules result with the LLM verdict; mark how we got
        # here in `matched_rule` so logs/telemetry can audit it.
        return Classification(
            intent=intent,
            confidence=0.7,
            matched_rule=f"llm_fallback:{rules.matched_rule}",
            target_student_handle=rules.target_student_handle,
        )


def _parse_intent(raw: str, vocab: Iterable[Intent]) -> Intent | None:
    """Lenient JSON parsing — the LLM occasionally adds backticks or notes."""
    text = raw.strip()
    # Strip code fences if the model emitted them anyway
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Try strict JSON first
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("intent"), str):
            return _snap(obj["intent"], vocab)
    except json.JSONDecodeError:
        pass

    # Fallback: look for any intent value verbatim in the response.
    lowered = text.lower()
    for intent in vocab:
        if intent.value in lowered:
            return intent
    return None


def _snap(candidate: str, vocab: Iterable[Intent]) -> Intent | None:
    candidate = candidate.strip().lower()
    for intent in vocab:
        if intent.value == candidate:
            return intent
    return None
