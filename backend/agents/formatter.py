from __future__ import annotations

from typing import Any

from backend.agents.state import MentorState

_NO_DATA = (
    "I don't have enough data to answer that yet. "
    "Please ask your teacher to upload assessment and attendance data."
)


_CITE_RE = __import__("re").compile(r"\[#(\d+)\]")


class FormatterAgent:
    def run(self, state: MentorState) -> dict[str, Any]:
        # 1. Guardrail refusal short-circuits everything else.
        refusal = (state.get("refusal_message") or "").strip()
        if refusal:
            return {"final_response": refusal}

        response = (state.get("llm_response") or "").strip()
        if not response:
            response = _NO_DATA

        # 2. Strip orphan [#N] tokens. The model sometimes invents citation
        # markers (we observed it in early smoke tests). If a marker
        # doesn't map to a real chunk, scrub it from the visible response
        # so users don't see dangling brackets.
        chunks = state.get("rag_chunks") or []
        max_idx = len(chunks)
        if "[#" in response:
            def _scrub(m):
                try:
                    idx = int(m.group(1))
                except ValueError:
                    return ""
                return m.group(0) if 1 <= idx <= max_idx else ""
            response = _CITE_RE.sub(_scrub, response)
            # collapse any double-spaces left behind by removed tokens
            response = response.replace("  ", " ").replace(" .", ".").replace(" ,", ",")

        # 3. Citation appendix — only when RAG chunks contributed AND the
        # model actually referenced one via [#N]. We don't dump unused
        # sources, that's noise.
        if chunks and "[#" in response:
            cited = _used_citations(response, max_idx)
            if cited:
                lines = ["", "Sources:"]
                for i in cited:
                    c = chunks[i - 1]
                    title = c.get("title") or c.get("kind") or "source"
                    lines.append(f"  [#{i}] {title}")
                response = response + "\n".join(lines)

        if not state.get("validation_passed") and not refusal:
            response += (
                "\n\n_Note: This response could not be fully verified against the data. "
                "Please treat specific numbers with caution._"
            )
        return {"final_response": response}


def _used_citations(text: str, max_idx: int) -> list[int]:
    found = set()
    for m in _CITE_RE.finditer(text):
        try:
            i = int(m.group(1))
        except ValueError:
            continue
        if 1 <= i <= max_idx:
            found.add(i)
    return sorted(found)
