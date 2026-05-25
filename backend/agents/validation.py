from __future__ import annotations

import re
from typing import Any

from backend.agents.state import MentorState
from backend.core.config import get_settings
from backend.llm.ollama import get_ollama

_SYSTEM = (
    "You are a strict fact-checker. Given a data context and an AI-generated response, "
    "determine if the response contradicts or fabricates data not present in the context. "
    "Reply with exactly one word: PASS if the response is consistent with the data, "
    "FAIL if it contradicts or invents data."
)


# Patterns that would indicate the LLM leaked PII / unsafe content into
# the response. The Intent Fence blocks the *request*; this is the
# matching belt-and-braces check on the *output*.
_LEAK_PATTERNS = [
    re.compile(r"\b\d{10}\b"),                        # 10-digit phone
    re.compile(r"\b\d{3}[- ]?\d{3}[- ]?\d{4}\b"),     # US/IN-style number
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),         # Aadhaar-shape
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),             # SSN
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),      # email address
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),            # PAN
]


class ValidationAgent:
    async def run(self, state: MentorState) -> dict[str, Any]:
        # Skip validation entirely on guardrail refusals — the response
        # is a hand-written refusal string, not an LLM output.
        if state.get("refusal_message"):
            return {"validation_passed": True, "retry_count": state.get("retry_count", 0)}

        response = state.get("llm_response", "").strip()
        retry_count = state.get("retry_count", 0)

        if len(response) < 10:
            return {"validation_passed": False, "retry_count": retry_count + 1}

        # 1. Output PII leak check — fail closed if the model emitted any
        # contact / id-shaped string. These never appear in our data
        # context, so any match is a hallucination or worse.
        for pat in _LEAK_PATTERNS:
            if pat.search(response):
                return {"validation_passed": False, "retry_count": retry_count + 1}

        # 2. LLM-as-judge groundedness check against the assembled context.
        ollama = get_ollama()
        check = (
            f"DATA CONTEXT:\n{state['user_prompt']}\n\n"
            f"AI RESPONSE:\n{response}\n\n"
            "Does the AI response contradict or fabricate data? Reply PASS or FAIL."
        )
        # The judge only emits PASS/FAIL — cap output tokens so this second
        # per-turn LLM call returns fast instead of generating a full answer.
        verdict = await ollama.complete(
            _SYSTEM, check,
            num_predict=get_settings().ollama_validation_num_predict,
        )
        passed = "PASS" in verdict.upper()
        return {
            "validation_passed": passed,
            "retry_count": retry_count + (0 if passed else 1),
        }
