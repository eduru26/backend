from __future__ import annotations

from typing import Any

from backend.agents.state import MentorState
from backend.llm.ollama import get_ollama


class InsightsAgent:
    async def run(self, state: MentorState) -> dict[str, Any]:
        ollama = get_ollama()
        response = await ollama.complete(state["system_prompt"], state["user_prompt"])
        return {"llm_response": response}
