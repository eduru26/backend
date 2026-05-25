"""Grounding validator for paper-generator questions.

Each LLM-generated question must be answerable from the textbook chunks
that were fed to it as context. We enforce that with two cheap checks:

  1. Embedding-similarity gate (always run)
       Embed (question + answer_key) and compare against the chunk
       vectors using cosine similarity. The max similarity over all
       chunks is the question's grounding score. Below `min_sim` the
       question is rejected outright.

  2. LLM judge (only on borderline cases)
       When `min_sim <= max_sim < judge_high`, ask the LLM "is this
       answer derivable from the context — PASS or FAIL?". Borderline
       similarity often means the question paraphrases the context but
       drifts on facts; the judge catches that without the cost of
       judging every question.

Returns the surviving questions in the input order with a
`_grounding_score` field added.

The bands (`min_sim`, `judge_high`) are config-driven via
`settings.grounding_*` so they can be tuned without code change. With
nomic-embed-text-v2-moe (768-d, multilingual) the empirical bands that
work for school-textbook prose are roughly 0.40-0.45 (reject below) /
0.60+ (accept without judge). Defaults reflect that.
"""
from __future__ import annotations

import math
from typing import Any

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 on zero-norm vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


async def _llm_judge(ollama: Any, context: str, question: str, answer: str) -> bool:
    """Single-token PASS/FAIL check via the small model."""
    system = (
        "You are a strict fact-checker. Given a textbook context and a "
        "question with its model answer, decide whether the answer is "
        "explicitly supported by the context. Reply with exactly one word: "
        "PASS if the answer is supported, FAIL otherwise."
    )
    user = (
        f"TEXTBOOK CONTEXT:\n{context[:1800]}\n\n"
        f"QUESTION: {question}\n"
        f"ANSWER: {answer}\n\n"
        "Is the answer supported by the context? Reply PASS or FAIL."
    )
    try:
        verdict = await ollama.complete(
            system, user,
            num_predict=get_settings().ollama_validation_num_predict,
        )
    except Exception as exc:  # noqa: BLE001
        # Judge unavailable → treat borderline as PASS so we don't drop
        # plausibly-grounded questions on a transient Ollama hiccup.
        logger.warning("grounding.judge_unavailable", error=str(exc))
        return True
    return "PASS" in (verdict or "").upper()


async def validate_questions(
    *,
    embedder: Any,
    ollama: Any,
    questions: list[dict],
    chunks: list[Any],
) -> list[dict]:
    """Filter `questions` to those grounded in the supplied chunks.

    Mutates surviving question dicts to include `_grounding_score`
    (max cosine similarity vs. any chunk). Returns the surviving list
    in the original order.
    """
    if not questions or not chunks:
        return []

    settings = get_settings()
    min_sim = float(getattr(settings, "grounding_min_sim", 0.45) or 0.45)
    judge_high = float(getattr(settings, "grounding_judge_high", 0.60) or 0.60)

    # Embed chunks first (Redis-cached → re-runs are nearly free).
    chunk_texts = [getattr(c, "content", "") or "" for c in chunks]
    chunk_vecs = await embedder.embed_batch(chunk_texts)

    # Embed each question's (text + answer) so an answer that quotes the
    # textbook lifts borderline questions above the threshold.
    q_inputs: list[str] = []
    for q in questions:
        qt = str(q.get("question_text", "")).strip()
        ak = str(q.get("answer_key", "")).strip()
        q_inputs.append(f"{qt}\n{ak}" if ak else qt)
    q_vecs = await embedder.embed_batch(q_inputs)

    context_blob = "\n\n".join(chunk_texts)[:2400]
    survivors: list[dict] = []

    for q, qvec in zip(questions, q_vecs):
        max_sim = 0.0
        for cvec in chunk_vecs:
            s = _cosine(qvec, cvec)
            if s > max_sim:
                max_sim = s

        if max_sim < min_sim:
            logger.info("grounding.reject",
                         reason="below_min_sim",
                         max_sim=round(max_sim, 3),
                         q=str(q.get("question_text", ""))[:80])
            continue

        if max_sim < judge_high:
            ok = await _llm_judge(
                ollama,
                context_blob,
                str(q.get("question_text", "")),
                str(q.get("answer_key", "")),
            )
            if not ok:
                logger.info("grounding.reject",
                             reason="judge_fail",
                             max_sim=round(max_sim, 3),
                             q=str(q.get("question_text", ""))[:80])
                continue

        q["_grounding_score"] = round(float(max_sim), 4)
        survivors.append(q)

    return survivors
