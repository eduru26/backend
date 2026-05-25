from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)


def extract_json(raw: str) -> Any:
    """Parse a JSON value from an LLM response.

    Small local models often wrap JSON in ```code fences``` or add a line
    of prose. This strips fences, then tries a strict parse, then falls
    back to the first balanced {...} / [...] span. Raises ValueError when
    nothing parseable is found so callers can retry or fail cleanly.
    """
    text = (raw or "").strip()
    text = _FENCE.sub("", text).replace("```", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError("no parseable JSON in LLM response")
