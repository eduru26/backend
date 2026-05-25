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


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """Last-resort splitter for a run of text with no paragraph or sentence
    break points (poems, tables, OCR blobs) — slices it into fixed `size`
    windows so no chunk can overflow the embedding model's context."""
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step)]


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
            continue
        for sentence in _SENT_BREAK.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= target_chars:
                units.append(sentence)
            else:
                # a sentence/run longer than the target — force-split so no
                # chunk can overflow the embedder's context window
                units.extend(_hard_split(sentence, target_chars, overlap_chars))

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
