"""AI question-paper generator.

Given an Assessment created with `status='generating'` and a populated
`generation_meta`, this fills it with `Question` rows: for every template
section it allocates questions across the teacher's selected topics
(by weightage for semester exams, evenly for unit tests), retrieves
textbook chunks for each topic from Qdrant, and prompts the LLM to
write questions grounded in that content.

Runs inside the arq worker (`generate_paper_job`). Per-topic LLM calls
run concurrently with bounded fan-out so a typical 5-topic paper takes
~10-12s instead of ~50-60s. Topics that fail or return zero questions
trigger a make-up redistribution across the remaining topics so the
section still hits its target count.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.grounding import validate_questions
from backend.core.logging import get_logger
from backend.db.models import (
    Assessment, Chapter, Question, QuestionDifficulty, QuestionPaperTemplate, Topic,
)
from backend.llm.json_io import extract_json
from backend.llm.ollama import get_ollama
from backend.rag.embeddings import get_embedder
from backend.rag.qdrant_store import get_qdrant_store

logger = get_logger(__name__)

# Concurrency cap for per-topic LLM calls. Ollama serialises generation
# server-side anyway, so cranking this beyond a handful just queues there.
_MAX_PARALLEL_TOPICS = 4

_GROUNDING_TAIL = (
    " ONLY use facts explicitly supported by the TEXTBOOK CONTEXT. Do "
    "not invent or pull from outside knowledge. If the context cannot "
    "support the requested count, return fewer questions; return an "
    "empty list if it cannot support any."
)

_SYS_BY_TYPE: dict[str, str] = {
    "mcq": (
        "You are an exam author. Write MULTIPLE-CHOICE questions. Each "
        "`question_text` must include the stem followed by exactly four "
        "options labelled (a)–(d), one per line. The `answer_key` must be "
        "a single letter a/b/c/d identifying the correct option."
        + _GROUNDING_TAIL
    ),
    "fill_blank": (
        "You are an exam author. Write FILL-IN-THE-BLANK questions. Each "
        "`question_text` must be one sentence with a single ____ (four "
        "underscores) where the missing word or phrase belongs. The "
        "`answer_key` is the missing word or phrase only."
        + _GROUNDING_TAIL
    ),
    "true_false": (
        "You are an exam author. Write TRUE/FALSE statements. Each "
        "`question_text` is one declarative sentence the student must "
        "mark true or false. The `answer_key` is exactly 'True' or "
        "'False'."
        + _GROUNDING_TAIL
    ),
    "one_word": (
        "You are an exam author. Write ONE-WORD-ANSWER questions — each "
        "answerable with a single word or short phrase (at most 5 words). "
        "The `answer_key` is that one word / phrase."
        + _GROUNDING_TAIL
    ),
    "short_answer": (
        "You are an exam author. Write SHORT-ANSWER questions — each "
        "answerable in 2-3 sentences. The `answer_key` is a brief model "
        "answer (2-3 sentences)."
        + _GROUNDING_TAIL
    ),
    "long_answer": (
        "You are an exam author. Write LONG-ANSWER questions — each "
        "requiring a detailed paragraph or essay-style response. The "
        "`answer_key` is an outline or model paragraph the marker can "
        "use as a rubric."
        + _GROUNDING_TAIL
    ),
    # Legacy umbrella — old templates may still have "objective" sections.
    # Map to MCQ-style behaviour (the most common objective form) so they
    # still produce something usable.
    "objective": (
        "You are an exam author. Write OBJECTIVE questions — each "
        "answerable in a word or short phrase (multiple choice, "
        "fill-in-the-blank, one-line, or true/false). The `answer_key` "
        "is the correct option or short answer."
        + _GROUNDING_TAIL
    ),
}

# Default difficulty mix per question. Templates don't carry this yet, so
# we apply a sensible default (40% easy, 40% medium, 20% hard) and round
# via the same largest-remainder method we use for topic allocation.
_DEFAULT_DIFFICULTY_MIX: dict[str, float] = {
    "easy": 0.4, "medium": 0.4, "hard": 0.2,
}


def _allocate(total: int, weights: list[float]) -> list[int]:
    """Distribute `total` integer units across slots by weight, using the
    largest-remainder method so the allocations always sum to `total`."""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    wsum = sum(weights)
    if wsum <= 0:
        weights = [1.0] * n
        wsum = float(n)
    raw = [total * w / wsum for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    order = sorted(range(n), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in range(remainder):
        floors[order[i % n]] += 1
    return floors


def _mark_mcq_correct(question_text: str, answer_key: str) -> str:
    """Embed a ✓ on the AI-picked correct option of an MCQ question.

    The MCQ editor in the teacher UI parses `question_text` to populate
    its stem + four option inputs, and reads the ✓ marker after the
    option letter as "this is the correct answer". The AI emits the
    correct letter (a/b/c/d) in a separate `answer_key` field, so we
    merge it into `question_text` here before persisting so a teacher
    opening the paper sees the right radio pre-selected.

    Tolerates noise in either field — if the answer letter doesn't
    match any option line, the text is returned unchanged.
    """
    import re as _re
    letter = (answer_key or "").strip().lower()
    if letter not in {"a", "b", "c", "d"}:
        return question_text
    # Match lines that look like "(a) ..." / "a) ..." / "a. ..." etc.
    head = _re.compile(
        rf"^(\s*\(?{letter}\)?\s*[).:\-]?\s+.*?)(\s*[✓*])?\s*$",
        _re.IGNORECASE,
    )
    out_lines = []
    marked = False
    for line in question_text.splitlines():
        m = head.match(line)
        if m and not marked:
            out_lines.append(f"{m.group(1).rstrip()} ✓")
            marked = True
        else:
            out_lines.append(line)
    return "\n".join(out_lines) if marked else question_text


def _difficulty_plan(count: int) -> list[QuestionDifficulty]:
    """Return a length-`count` list of difficulties matching the default mix.
    Repeated when count > sum-of-buckets via largest-remainder."""
    if count <= 0:
        return []
    weights = [_DEFAULT_DIFFICULTY_MIX["easy"],
               _DEFAULT_DIFFICULTY_MIX["medium"],
               _DEFAULT_DIFFICULTY_MIX["hard"]]
    alloc = _allocate(count, weights)
    plan: list[QuestionDifficulty] = (
        [QuestionDifficulty.easy] * alloc[0]
        + [QuestionDifficulty.medium] * alloc[1]
        + [QuestionDifficulty.hard] * alloc[2]
    )
    return plan


class PaperGenerator:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate(self, assessment_id: uuid.UUID) -> None:
        assessment = await self._session.get(Assessment, assessment_id)
        if assessment is None:
            raise RuntimeError(f"assessment {assessment_id} not found")
        meta: dict[str, Any] = dict(assessment.generation_meta or {})
        # Reset run-derived flags so a regenerate doesn't inherit the
        # previous run's fallback verdict — the worker will set these
        # again at the end of generate() based on what actually happens.
        meta["mode"] = "ai"
        meta.pop("ai_fallback", None)
        template = await self._session.get(
            QuestionPaperTemplate, uuid.UUID(meta["template_id"])
        )
        if template is None:
            raise RuntimeError("template not found")

        subject_name: str = meta.get("subject_name", "")
        topic_ids: list[str] = meta.get("topic_ids", [])
        weightage: dict = meta.get("weightage", {}) or {}
        topic_weights: dict = weightage.get("topics", {}) if weightage else {}
        grade: int | None = (
            int(meta["grade"]) if meta.get("grade") is not None else None
        )
        tenant_id = uuid.UUID(meta["tenant_id"])

        # Resolve topic rows + their chapters (chapter name sharpens the
        # retrieval query — "Bar Graphs" alone is ambiguous, "Bar Graphs
        # (Mathematics / Data Handling and Presentation)" is not).
        topics: list[Topic] = []
        topic_chapter_name: dict[uuid.UUID, str | None] = {}
        for tid in topic_ids:
            t = await self._session.get(Topic, uuid.UUID(tid))
            if t is None:
                continue
            topics.append(t)
            ch_name: str | None = None
            if t.chapter_id is not None:
                ch = await self._session.get(Chapter, t.chapter_id)
                ch_name = ch.name if ch else None
            topic_chapter_name[t.id] = ch_name
        if not topics:
            raise RuntimeError("no valid topics selected")

        embedder = get_embedder()
        qstore = get_qdrant_store()
        ollama = get_ollama()

        # Preserve any teacher-edited questions across regenerate — only
        # wipe rows whose user_edited flag is still false.
        preserved = (await self._session.execute(
            select(Question).where(
                Question.assessment_id == assessment_id,
                Question.user_edited.is_(True),
            )
        )).scalars().all()
        await self._session.execute(
            delete(Question).where(
                Question.assessment_id == assessment_id,
                Question.user_edited.is_(False),
            )
        )

        warnings: list[str] = []
        answer_keys: dict[str, str] = {}
        topics_no_context: list[str] = []
        question_no = len(preserved)
        # Track which question_no slots are taken by preserved rows so
        # we don't collide on the unique (assessment_id, question_no).
        used_nos: set[int] = {p.question_no for p in preserved}

        sem = asyncio.Semaphore(_MAX_PARALLEL_TOPICS)

        for section in template.sections or []:
            section_no = int(section.get("section_no", 1))
            qtype = section.get("question_type", "short_answer")
            answer_count = int(section.get("question_count", 1))
            optional_of = section.get("optional_of")
            gen_count = int(optional_of) if optional_of else answer_count
            is_optional = optional_of is not None
            marks = Decimal(str(section.get("marks_per_question", 1)))

            weights = [float(topic_weights.get(str(t.id), 0.0)) for t in topics]
            alloc = _allocate(gen_count, weights)

            # First pass — fire all topic calls concurrently with bounded
            # fan-out. The third tuple slot is the list of TextbookHits
            # used as context: empty list ⇒ topic has no coverage, skip.
            async def _bounded(t: Topic, n: int) -> tuple[Topic, list[dict], list[Any]]:
                if n <= 0:
                    return t, [], []
                async with sem:
                    try:
                        qs, hits = await self._generate_for_topic(
                            embedder=embedder, qstore=qstore, ollama=ollama,
                            tenant_id=tenant_id, grade=grade,
                            subject_name=subject_name,
                            chapter_name=topic_chapter_name.get(t.id),
                            topic=t, qtype=qtype, count=n,
                        )
                        return t, qs, hits
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("paper_gen.topic_failed",
                                       topic=t.name, error=str(exc))
                        warnings.append(
                            f"{section.get('title', 'Section')}/{t.name}: {exc}")
                        return t, [], []

            first_pass = await asyncio.gather(*[
                _bounded(t, n) for t, n in zip(topics, alloc)
            ])

            # Shortfall: total produced < gen_count → redistribute the
            # missing count across topics that DID return something, with
            # a small extra ask per topic. This avoids a section ending up
            # short because one topic LLM call failed. Uncovered topics
            # (empty hits) are intentionally excluded as survivors.
            produced = sum(len(qs) for _, qs, _ in first_pass)
            shortfall = max(0, gen_count - produced)
            extras_per_topic: dict[uuid.UUID, int] = {}
            if shortfall:
                survivors = [t for t, qs, _ in first_pass if qs]
                if survivors:
                    extra_alloc = _allocate(
                        shortfall, [1.0] * len(survivors))
                    for t, n in zip(survivors, extra_alloc):
                        if n > 0:
                            extras_per_topic[t.id] = n

            if extras_per_topic:
                makeup = await asyncio.gather(*[
                    _bounded(t, n) for t, n in zip(
                        [t for t, _, _ in first_pass if t.id in extras_per_topic],
                        [extras_per_topic[t.id] for t, _, _ in first_pass
                         if t.id in extras_per_topic],
                    )
                ])
                by_id = {t.id: (qs, hits) for t, qs, hits in makeup}
                augmented: list[tuple[Topic, list[dict], list[Any]]] = []
                for t, qs, hits in first_pass:
                    if t.id in by_id:
                        more, hits2 = by_id[t.id]
                        qs = qs + more
                        # Union the two hit lists (dedup by point_id) so
                        # later grounding validation has the full context.
                        seen = {h.point_id for h in hits if h.point_id}
                        for h in hits2:
                            if h.point_id not in seen:
                                hits.append(h)
                                seen.add(h.point_id)
                    augmented.append((t, qs, hits))
                first_pass = augmented

            # Plan a section-wide difficulty mix, then drain in topic order.
            section_total = sum(len(qs) for _, qs, _ in first_pass)
            diff_plan = _difficulty_plan(section_total)
            diff_iter = iter(diff_plan)

            for topic, questions, hits in first_pass:
                # Topic was selected but produced nothing (no coverage or
                # LLM failure) — surface to the teacher.
                if not questions and not hits:
                    topics_no_context.append(topic.name)
                # Collect the point ids of the chunks that grounded this
                # topic; the Question rows below cite them so the UI can
                # show a "Show source" drawer per question.
                source_ids = [h.point_id for h in hits if h.point_id]
                for q in questions:
                    question_no = _next_unused_no(question_no, used_nos)
                    used_nos.add(question_no)
                    self._session.add(Question(
                        id=uuid.uuid4(),
                        assessment_id=assessment_id,
                        question_no=question_no,
                        topic_id=topic.id,
                        max_marks=marks,
                        difficulty=next(diff_iter, _difficulty(q.get("difficulty"))),
                        question_text=q.get("question_text", "").strip(),
                        section_no=section_no,
                        is_optional=is_optional,
                        source_chunk_ids=source_ids or None,
                        grounding_score=float(q["_grounding_score"])
                            if isinstance(q.get("_grounding_score"), (int, float))
                            else None,
                    ))
                    if q.get("answer_key"):
                        answer_keys[str(question_no)] = str(q["answer_key"])[:500]

        # Fallback to manual mode if the AI couldn't produce anything
        # for this assessment — no textbook coverage on any selected
        # topic, every topic LLM call failed, all candidates rejected
        # by the grounding validator, etc. The teacher still needs a
        # paper, so we seed blank Question rows from the template and
        # flip mode to "manual". The UI hides AI controls automatically.
        await self._session.flush()  # make new inserts visible
        total_q = (await self._session.execute(
            select(func.count(Question.id))
            .where(Question.assessment_id == assessment_id)
        )).scalar_one() or 0
        if total_q == 0:
            logger.warning("paper_gen.ai_fallback_to_manual",
                           assessment_id=str(assessment_id),
                           ungrounded_topics=len(set(topics_no_context)))
            self._seed_blank_questions(
                assessment_id=assessment_id,
                template=template,
                topics=topics,
                topic_weights=topic_weights,
            )
            meta["mode"] = "manual"
            meta["ai_fallback"] = True
            warnings.append(
                "AI couldn't ground any questions in the textbook — "
                "switched to manual entry. Fill in the rows below.",
            )

        # Renumber: preserved + new, in section_no/question_no order, to
        # restore a clean 1..N sequence after the deletes left holes.
        await self._renumber(assessment_id)

        assessment.max_marks = Decimal(str(template.total_marks))
        assessment.status = "draft"
        meta["answer_keys"] = answer_keys
        meta["warnings"] = warnings
        meta["topics_no_context"] = sorted(set(topics_no_context))
        meta["generated_questions"] = question_no
        meta["preserved_questions"] = len(preserved)
        assessment.generation_meta = meta
        await self._session.flush()
        logger.info("paper_gen.done", assessment_id=str(assessment_id),
                    questions=question_no, warnings=len(warnings),
                    preserved=len(preserved),
                    ungrounded_topics=len(set(topics_no_context)),
                    ai_fallback=bool(meta.get("ai_fallback")))

    def _seed_blank_questions(
        self,
        *,
        assessment_id: uuid.UUID,
        template: QuestionPaperTemplate,
        topics: list[Topic],
        topic_weights: dict,
    ) -> None:
        """Insert blank Question rows for an AI-fallback paper.

        Mirrors the manual-paper flow used for language subjects: each
        template section's `question_count` is allocated across the
        teacher's selected topics by weight (or evenly when no weights
        are set), and one blank Question row is added per slot. The
        teacher fills the text in via the standard edit endpoint.
        """
        if not topics or not template.sections:
            return
        weights = [float(topic_weights.get(str(t.id), 1.0)) for t in topics]
        question_no = 1
        for section in template.sections or []:
            section_no = int(section.get("section_no", 1))
            answer_count = int(section.get("question_count", 1))
            optional_of = section.get("optional_of")
            gen_count = int(optional_of) if optional_of else answer_count
            is_optional = optional_of is not None
            marks = Decimal(str(section.get("marks_per_question", 1)))
            alloc = _allocate(gen_count, weights)
            for topic, n in zip(topics, alloc):
                for _ in range(n):
                    self._session.add(Question(
                        id=uuid.uuid4(),
                        assessment_id=assessment_id,
                        question_no=question_no,
                        topic_id=topic.id,
                        max_marks=marks,
                        difficulty=QuestionDifficulty.medium,
                        question_text=None,
                        section_no=section_no,
                        is_optional=is_optional,
                    ))
                    question_no += 1

    async def _generate_for_topic(
        self,
        *,
        embedder: Any,
        qstore: Any,
        ollama: Any,
        tenant_id: uuid.UUID,
        grade: int | None,
        subject_name: str,
        chapter_name: str | None,
        topic: Topic,
        qtype: str,
        count: int,
    ) -> tuple[list[dict], list[Any]]:
        """Generate `count` questions for one topic.

        Returns (questions, hits) where `hits` is the retrieved chunks
        used to ground the prompt. Empty `hits` means the topic has no
        textbook coverage — the caller skips it (strict no-coverage
        policy, never falls back to general knowledge).
        """
        # Concatenate subject + chapter into the retrieval query so a
        # generic topic name like "Introduction" doesn't pull cross-topic
        # garbage. The embedding model is multilingual and order-tolerant.
        retrieval_query = " ".join(filter(None, [
            subject_name, chapter_name, topic.name,
        ])) or topic.name
        qvec = await embedder.embed(retrieval_query)

        # Two-stage hard filter: try topic-scoped first; if that's too
        # sparse, broaden to chapter; never go subject-wide because that
        # is what lets unrelated chapters bleed into the prompt.
        hits: list[Any] = []
        if topic.id is not None:
            hits = await qstore.search_textbook(
                query_vector=qvec, tenant_id=tenant_id,
                subject=subject_name or None, grade=grade,
                topic_ids=[topic.id], limit=4,
            )
        if len(hits) < 2 and getattr(topic, "chapter_id", None):
            hits = await qstore.search_textbook(
                query_vector=qvec, tenant_id=tenant_id,
                subject=subject_name or None, grade=grade,
                chapter_ids=[topic.chapter_id], limit=4,
            )
        if not hits:
            # Strict: no coverage → no question. Caller adds the topic to
            # topics_no_context and the UI nudges the admin to upload more.
            logger.info("paper_gen.skip_uncovered_topic",
                         topic=topic.name, chapter=chapter_name)
            return [], []

        context = "\n\n".join(h.content for h in hits)[:2400]
        context_block = f"TEXTBOOK CONTEXT:\n{context}"
        system = _SYS_BY_TYPE.get(qtype, _SYS_BY_TYPE["short_answer"]) + (
            " Return STRICT JSON only as an object of the form "
            '{"questions":[{"question_text": "...", '
            '"difficulty":"easy"|"medium"|"hard", "answer_key":"..."}]}. '
            "No prose, no markdown, no extra keys."
        )
        chapter_part = f" Chapter: {chapter_name}." if chapter_name else ""
        user = (
            f"{context_block}\n\n"
            f"Subject: {subject_name or 'General'}.{chapter_part} "
            f"Topic: {topic.name}.\n"
            f"Write up to {count} question(s) on this topic. "
            "Every answer_key must paraphrase or quote the TEXTBOOK "
            "CONTEXT above. Do not introduce facts that are not in it. "
            'Return as {"questions":[...]}.'
        )
        raw = await ollama.complete(system, user, num_predict=900, json_mode=True)
        try:
            parsed = extract_json(raw)
        except ValueError:
            logger.warning("paper_gen.bad_json", topic=topic.name, raw=raw[:300])
            return [], hits
        items = _items_from_parsed(parsed)
        if not items:
            logger.warning("paper_gen.no_items", topic=topic.name, raw=raw[:300])
            return [], hits
        candidates: list[dict] = []
        for item in items[:count]:
            if isinstance(item, dict) and str(item.get("question_text", "")).strip():
                candidates.append(item)
        if not candidates:
            logger.warning("paper_gen.empty_after_filter",
                           topic=topic.name, raw_items=str(items)[:300])
            return [], hits

        # Grounding gate — rejects questions whose answer isn't supported
        # by the chunks we fed the LLM. Survivors carry `_grounding_score`,
        # which the caller persists on the Question row.
        try:
            grounded = await validate_questions(
                embedder=embedder, ollama=ollama,
                questions=candidates, chunks=hits,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper_gen.grounding_failed",
                           topic=topic.name, error=str(exc))
            grounded = candidates  # fail-open so a validator outage
            # doesn't block the whole paper; UI shows null grounding_score.
        if not grounded:
            logger.info("paper_gen.all_rejected_by_grounding",
                         topic=topic.name, candidates=len(candidates))
        return grounded, hits

    async def _renumber(self, assessment_id: uuid.UUID) -> None:
        """Restore a contiguous 1..N question_no sequence after edits +
        partial deletes. Sort by (section_no, current question_no)."""
        rows = (await self._session.execute(
            select(Question).where(Question.assessment_id == assessment_id)
            .order_by(Question.section_no.nullslast(), Question.question_no)
        )).scalars().all()
        # Two-phase renumber: assign large temp numbers first to avoid
        # tripping the (assessment_id, question_no) unique constraint mid-update.
        for i, q in enumerate(rows, start=1):
            q.question_no = 10_000 + i
        await self._session.flush()
        for i, q in enumerate(rows, start=1):
            q.question_no = i
        await self._session.flush()


def _next_unused_no(prev: int, used: set[int]) -> int:
    n = prev + 1
    while n in used:
        n += 1
    return n


def _items_from_parsed(parsed: Any) -> list:
    """Unwrap the question list from whatever shape the LLM produced.

    Accepted shapes:
      - a bare list
      - {"questions": [...]}                (the contract)
      - {"items"|"list"|"data"|"results": [...]}   (common alternates)
      - any dict whose first list-valued field looks like questions
      - {"question_text": ...}              (single object → wrap)
    """
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    if "question_text" in parsed:
        return [parsed]
    for key in ("questions", "items", "list", "data", "results"):
        v = parsed.get(key)
        if isinstance(v, list) and v:
            return v
    for v in parsed.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "question_text" in v[0]:
            return v
    return []


def _difficulty(value: Any) -> QuestionDifficulty:
    raw = str(value or "").strip().lower()
    if raw == "easy":
        return QuestionDifficulty.easy
    if raw == "hard":
        return QuestionDifficulty.hard
    return QuestionDifficulty.medium
