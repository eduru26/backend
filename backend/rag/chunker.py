from __future__ import annotations

import re

# Defaults tuned for nomic-embed-text (max 8192 tokens, but quality holds
# around 200-500 tokens of context). ~600 characters ≈ ~150 tokens; the
# 100-char overlap preserves cross-boundary references.
DEFAULT_CHUNK_CHARS = 600
DEFAULT_OVERLAP_CHARS = 100
MIN_CHUNK_CHARS = 80


_PARA_BREAK = re.compile(r"\n{2,}")
_SENT_BREAK = re.compile(r"(?<=[.!?])\s+")


def chunk_text(
    text: str,
    *,
    target_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Split text into ~target_chars chunks with a small overlap.

    Strategy: split on paragraph breaks first, then within each paragraph
    on sentence boundaries, accumulating until we reach `target_chars`.
    Adjacent chunks share `overlap_chars` of trailing text from the
    previous chunk so cross-boundary phrases (numeric tables, references)
    stay retrievable.

    This is intentionally a hand-rolled splitter — adding `langchain-
    text-splitters` for ~30 lines of logic is more dependency surface
    than it's worth.
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in _PARA_BREAK.split(text) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= target_chars:
            units.append(para)
        else:
            sentences = _SENT_BREAK.split(para)
            units.extend(s.strip() for s in sentences if s.strip())

    chunks: list[str] = []
    buf = ""
    for unit in units:
        if not buf:
            buf = unit
            continue
        if len(buf) + 1 + len(unit) <= target_chars:
            buf = f"{buf} {unit}"
        else:
            chunks.append(buf)
            # carry overlap into the next chunk
            tail = buf[-overlap_chars:] if overlap_chars > 0 else ""
            buf = f"{tail} {unit}".strip() if tail else unit
    if buf and len(buf) >= MIN_CHUNK_CHARS:
        chunks.append(buf)
    elif buf and chunks:
        # last fragment is tiny — fold into the previous chunk
        chunks[-1] = f"{chunks[-1]} {buf}"
    elif buf:
        chunks.append(buf)

    return chunks
