"""Textbook PDF ingestion — docling-based.

End-to-end flow inside the dedicated ingest worker:

  1. extracting  — docling parses the PDF into a structured doc
  2. chunking    — split markdown by H1/H2 into chapters/topics,
                   then 600/100 windowed chunks per topic body
  3. embedding   — embed every chunk via Ollama and upsert to Qdrant
                   tagged with chapter_order / topic_name / chunk_type
  4. ready       — create a CurriculumProposal so the admin can review
                   the chapter outline before clicking Apply

Status + progress are flushed to Postgres at each stage so the admin
UI can show a progress bar without hitting Redis. Docling is heavy
(~2–3 GB layout models on first use); we import it lazily so the
backend image stays light.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.db.models import (
    CurriculumProposal, IngestStatus, Subject, TextbookIngestion,
)
from backend.rag.embeddings import get_embedder
from backend.rag.qdrant_store import get_qdrant_store

logger = get_logger(__name__)

# 600/100 windowed chunks match the backend's chunker so embeddings
# stay interchangeable across the ingest path and the live retriever.
_CHUNK_TARGET_CHARS = 600
_CHUNK_OVERLAP_CHARS = 100
_MAX_CHAPTERS = 60  # defensive cap for absurd docling outputs


# ─────────────────────────────────────────────────────────────────
# Public orchestration
# ─────────────────────────────────────────────────────────────────


class _IngestCancelled(Exception):
    """Raised when the admin cancelled (or deleted) an in-flight ingestion.

    Bubbled up to the worker job which treats it as a clean exit rather
    than a failure — the row is already in `cancelled` state (or gone),
    and the worker shouldn't overwrite that with `failed`.
    """


async def run_ingestion(session: AsyncSession, ingestion_id: uuid.UUID) -> None:
    """Run the full ingest pipeline for one queued TextbookIngestion.

    Caller (the arq worker job) is responsible for the outer session
    lifecycle and for catching exceptions to mark the row failed in a
    fresh session. Each stage commits so the admin UI sees progress
    and so we can detect a mid-flight cancellation between stages.
    """
    ing = await session.get(TextbookIngestion, ingestion_id)
    if ing is None:
        logger.warning("ingest.missing", ingestion_id=str(ingestion_id))
        return
    if ing.status != IngestStatus.queued:
        logger.info("ingest.skip_non_queued",
                    ingestion_id=str(ingestion_id), status=ing.status.value)
        return

    subject = await session.get(Subject, ing.subject_id)
    if subject is None:
        raise RuntimeError("subject row was deleted before ingest started")

    pdf_path = Path(ing.file_path)
    if not pdf_path.is_file():
        raise RuntimeError(f"PDF missing on disk: {pdf_path}")

    # 1. Docling extraction → markdown
    await _set_status(session, ing, IngestStatus.extracting, progress=5)
    markdown = await asyncio.to_thread(_docling_to_markdown, pdf_path)
    await _abort_if_cancelled(session, ingestion_id)
    if not markdown.strip():
        raise RuntimeError("docling produced empty markdown")
    await _set_status(session, ing, IngestStatus.extracting, progress=35)

    # 2. Chunking — chapters/topics from markdown headings,
    # then 600/100 windowed sub-chunks per topic body.
    await _set_status(session, ing, IngestStatus.chunking, progress=45)
    outline, body_chunks = _parse_markdown(markdown)
    if not outline:
        raise RuntimeError("no chapter headings detected in markdown")
    await _abort_if_cancelled(session, ingestion_id)
    await _set_status(session, ing, IngestStatus.chunking, progress=60)

    # 3. Embedding — fan out via embedder, batch into Qdrant.
    await _set_status(session, ing, IngestStatus.embedding, progress=65)
    await _embed_and_upsert(
        session=session, ingestion=ing, subject=subject,
        body_chunks=body_chunks,
    )
    await _abort_if_cancelled(session, ingestion_id)
    await _set_status(session, ing, IngestStatus.embedding, progress=90)

    # 4. Proposal — single row the admin reviews and applies.
    proposal = CurriculumProposal(
        id=uuid.uuid4(),
        tenant_id=ing.tenant_id,
        subject_id=subject.id,
        grade=ing.grade,
        title=ing.source_filename[:300],
        status="ready",
        outline_json={
            "chapters": outline,
            "source": ing.source_filename,
        },
    )
    session.add(proposal)
    ing.proposal_id = proposal.id
    await _set_status(session, ing, IngestStatus.ready, progress=100)
    logger.info("ingest.ready",
                ingestion_id=str(ing.id), source=ing.source_filename,
                chapters=len(outline),
                chunks=sum(len(c["chunks"]) for c in body_chunks))


async def _abort_if_cancelled(
    session: AsyncSession, ingestion_id: uuid.UUID,
) -> None:
    """Re-fetch the row; raise _IngestCancelled if it's gone or cancelled.

    Called between stages so the long docling / embed steps can't be
    interrupted, but everything between them can. The worker exits
    cleanly instead of marking the row failed.
    """
    # Use a no-cache lookup so we see the admin's status flip even if the
    # in-session row is still in the identity map.
    await session.commit()
    row = await session.get(TextbookIngestion, ingestion_id)
    if row is None:
        raise _IngestCancelled("ingestion row was deleted by the admin")
    if row.status == IngestStatus.cancelled:
        raise _IngestCancelled("ingestion was cancelled by the admin")


async def mark_ingestion_failed(
    session: AsyncSession, ingestion_id: uuid.UUID, error: str,
) -> None:
    """Mark a row failed in a fresh session — used by the worker on crash."""
    ing = await session.get(TextbookIngestion, ingestion_id)
    if ing is None:
        return
    ing.status = IngestStatus.failed
    ing.error = error[:1000]
    await session.flush()
    await session.commit()


# ─────────────────────────────────────────────────────────────────
# Docling — lazy import keeps the backend image lean
# ─────────────────────────────────────────────────────────────────


def _docling_to_markdown(pdf_path: Path) -> str:
    """Run docling on a single PDF and return markdown.

    Lazy-imported so the backend service (which never calls this) doesn't
    have to ship the 2–3 GB of layout / OCR models docling pulls down.
    Runs in a worker thread because docling is CPU-bound and synchronous.
    """
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    return result.document.export_to_markdown()


# ─────────────────────────────────────────────────────────────────
# Markdown → curriculum outline + content chunks
# ─────────────────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")


def _parse_markdown(md: str) -> tuple[list[dict], list[dict]]:
    """Turn docling's markdown into (outline, body_chunks).

    `outline` shape matches CurriculumProposal.outline_json:
        [{"name", "order", "topics": [str, ...]}]
    `body_chunks` is one entry per topic body (or chapter body if no
    topics): {"chapter_order", "topic_name", "chunks": [str, ...]}.

    Rules:
      - H1 / H2 → chapter (whichever appears first wins as "chapter level")
      - The next-deeper heading level under a chapter → topic
      - Body text between headings is windowed into 600/100 chunks
    """
    lines = md.splitlines()
    # First pass — collect (level, title, line_index) for every heading.
    headings: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = _clean_heading(m.group(2))
            if title:
                headings.append((level, title, i))
    if not headings:
        return [], []

    # Decide which level is "chapter" — the shallowest heading present.
    chapter_level = min(h[0] for h in headings)
    topic_level = chapter_level + 1

    outline: list[dict] = []
    body_chunks: list[dict] = []
    current_chapter: dict | None = None
    current_topic_name: str | None = None
    current_body_start: int | None = None

    def flush_body(end_line: int) -> None:
        nonlocal current_body_start
        if current_chapter is None or current_body_start is None:
            current_body_start = None
            return
        body = "\n".join(lines[current_body_start:end_line]).strip()
        current_body_start = None
        if not body:
            return
        pieces = _window_chunks(body, _CHUNK_TARGET_CHARS, _CHUNK_OVERLAP_CHARS)
        if not pieces:
            return
        body_chunks.append({
            "chapter_order": int(current_chapter["order"]),
            "topic_name": current_topic_name,
            "chunks": pieces,
        })

    for idx, (level, title, line_no) in enumerate(headings):
        next_line = (
            headings[idx + 1][2] if idx + 1 < len(headings) else len(lines)
        )
        if level == chapter_level:
            flush_body(line_no)
            if len(outline) >= _MAX_CHAPTERS:
                break
            current_chapter = {
                "name": title[:200], "order": len(outline) + 1, "topics": [],
            }
            outline.append(current_chapter)
            current_topic_name = None
            # Body between this chapter heading and the next heading is
            # chapter-level prose (intro paragraphs etc.). Capture it.
            current_body_start = line_no + 1
        elif level == topic_level and current_chapter is not None:
            flush_body(line_no)
            current_topic_name = title[:200]
            current_chapter["topics"].append(current_topic_name)
            current_body_start = line_no + 1
        else:
            # Deeper headings (H3+/H4) are folded into their parent
            # topic's body — we don't model them as curriculum nodes.
            continue
        # After the last heading, push the trailing body.
        if idx == len(headings) - 1:
            flush_body(next_line)

    return outline, body_chunks


def _clean_heading(text: str) -> str:
    """Normalise a heading — strip leading 'Chapter N:' style noise."""
    text = re.sub(r"^\s*(?:Chapter|Unit|Lesson)\s+\d+\s*[:.\-–]?\s*", "",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\d+(?:\.\d+)*\s+", "", text)  # "3.2 Foo" → "Foo"
    return text.strip()


def _window_chunks(text: str, target: int, overlap: int) -> list[str]:
    """Plain windowed splitter — paragraphs preserved, hard wraps avoided."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    out: list[str] = []
    step = max(1, target - overlap)
    i = 0
    while i < len(text):
        end = min(len(text), i + target)
        # snap end to nearest whitespace within the last 20% of the window
        if end < len(text):
            window_start = max(i + int(target * 0.8), i + 1)
            sp = text.rfind(" ", window_start, end)
            if sp > 0:
                end = sp
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        i = end - overlap if end - overlap > i else end
    return out


# ─────────────────────────────────────────────────────────────────
# Embedding + Qdrant upsert
# ─────────────────────────────────────────────────────────────────


async def _embed_and_upsert(
    *,
    session: AsyncSession,
    ingestion: TextbookIngestion,
    subject: Subject,
    body_chunks: list[dict],
) -> None:
    """Embed every chunk via Ollama and upsert as Qdrant points.

    Wipes any prior chunks for (tenant, source) so a re-upload starts
    clean rather than leaving orphans. Mirrors the old pack-import
    behaviour so the apply step's chapter/topic back-tagging works
    unchanged.
    """
    embedder = get_embedder()
    qstore = get_qdrant_store()

    # Build flat list of dicts the qdrant_store expects.
    payload: list[dict] = []
    flat_texts: list[str] = []
    flat_meta: list[tuple[int, str | None]] = []  # (chapter_order, topic)
    for entry in body_chunks:
        for chunk_text in entry["chunks"]:
            flat_texts.append(chunk_text)
            flat_meta.append((entry["chapter_order"], entry["topic_name"]))

    if not flat_texts:
        return

    # Batch embed for cache + speed; the embedder already serialises
    # internally so we don't need fan-out concurrency here.
    vectors = await embedder.embed_batch(flat_texts)

    for text_piece, vec, (ch_order, topic) in zip(flat_texts, vectors, flat_meta):
        payload.append({
            "content": text_piece,
            "embedding": vec,
            "chapter": ch_order,
            "topic": topic,
            "chunk_type": "content",
        })

    # Clean any prior upload of this PDF (same tenant + source) before
    # writing fresh chunks. Failure is logged but not fatal — qdrant
    # collection may not exist yet on a green install.
    try:
        await qstore.delete_textbook(
            tenant_id=ingestion.tenant_id, source=ingestion.source_filename,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest.qdrant_delete_failed",
                       source=ingestion.source_filename, error=str(exc))

    await qstore.upsert_textbook_chunks(
        tenant_id=ingestion.tenant_id,
        subject=subject.name,
        grade=ingestion.grade,
        title=ingestion.source_filename,
        source=ingestion.source_filename,
        uploaded_by=ingestion.uploaded_by,
        chunks=payload,
        file_sha256=ingestion.file_sha256,
    )


# ─────────────────────────────────────────────────────────────────
# Status helpers
# ─────────────────────────────────────────────────────────────────


async def _set_status(
    session: AsyncSession, ing: TextbookIngestion,
    status: IngestStatus, *, progress: int,
) -> None:
    ing.status = status
    ing.progress = max(0, min(100, int(progress)))
    await session.flush()
    await session.commit()


def get_textbook_storage_dir() -> Path:
    """Where uploaded PDFs land on disk inside both backend and worker."""
    s = get_settings()
    path = Path(getattr(s, "textbook_storage_path", "/data/textbook_pdfs"))
    path.mkdir(parents=True, exist_ok=True)
    return path
