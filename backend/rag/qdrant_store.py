"""Async Qdrant client for textbook chunks.

School-admin uploads write here; the teacher's paper generator reads from
here. Separate from the pgvector store (announcements/assignments/notes)
which keeps the existing RBAC chat-RAG path unchanged.

Single collection `textbook_chunks`, payload always tagged with the
`tenant_id` of the uploading school so cross-tenant search is impossible.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchAny, MatchValue,
    PayloadSchemaType, PointStruct, VectorParams,
)

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

# Deterministic UUIDv5 namespace so re-uploading the same textbook
# overwrites instead of duplicating its points.
_NAMESPACE = uuid.UUID("a7f3e1c8-6b2d-4e9a-9c1f-0a1b2c3d4e5f")


@dataclass(frozen=True)
class TextbookHit:
    score: float
    content: str
    tenant_id: str | None
    subject: str | None
    grade: int | None
    chapter: str | None
    title: str | None
    chunk_index: int
    # New curriculum-aware fields used by the paper generator for
    # grounded retrieval + source citations. `point_id` is the Qdrant
    # UUID so a question can reference the exact chunk it grounded on.
    point_id: str | None = None
    chapter_id: str | None = None
    topic_id: str | None = None
    chapter_order: int | None = None
    topic_name: str | None = None
    chunk_type: str = "content"
    source: str | None = None
    page: int | None = None


class QdrantStore:
    def __init__(self) -> None:
        s = get_settings()
        self._url = s.qdrant_url.rstrip("/")
        self._collection = s.qdrant_collection
        self._dim = s.vector_dim
        self._batch = s.qdrant_upsert_batch
        self._client: AsyncQdrantClient | None = None
        self._ensured = False

    async def _ready(self, wait_secs: int = 15) -> AsyncQdrantClient:
        if self._client is not None:
            return self._client
        # Qdrant boots fast but the depends_on healthcheck is a TCP probe;
        # /readyz is the real signal. Poll briefly so the first call after
        # backend boot doesn't race the server's collection load.
        async with httpx.AsyncClient(timeout=3.0) as http:
            import asyncio
            deadline = asyncio.get_event_loop().time() + wait_secs
            while True:
                try:
                    r = await http.get(f"{self._url}/readyz")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if asyncio.get_event_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Qdrant not ready at {self._url} after {wait_secs}s")
                await asyncio.sleep(0.75)
        self._client = AsyncQdrantClient(url=self._url)
        return self._client

    async def ensure_collection(self) -> None:
        if self._ensured:
            return
        client = await self._ready()
        if not await client.collection_exists(self._collection):
            await client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
            )
            # Payload indexes so tenant/subject/grade/chapter/topic filters
            # are O(log n) rather than scanning every point. Failure-tolerant:
            # re-creating the index on a hot collection raises, which we
            # swallow.
            for field, schema in (
                ("tenant_id",     PayloadSchemaType.KEYWORD),
                ("subject",       PayloadSchemaType.KEYWORD),
                ("grade",         PayloadSchemaType.INTEGER),
                ("source",        PayloadSchemaType.KEYWORD),
                ("file_sha256",   PayloadSchemaType.KEYWORD),
                # Curriculum tags — what makes the paper generator able to
                # hard-filter retrieval by chapter / topic and exclude
                # exercise chunks from question generation.
                ("chapter_id",    PayloadSchemaType.KEYWORD),
                ("topic_id",      PayloadSchemaType.KEYWORD),
                ("chapter_order", PayloadSchemaType.INTEGER),
                ("chunk_type",    PayloadSchemaType.KEYWORD),
            ):
                try:
                    await client.create_payload_index(
                        collection_name=self._collection,
                        field_name=field, field_schema=schema,
                    )
                except Exception:  # noqa: BLE001 — already-exists is fine
                    pass
        else:
            # The collection survives across embed-model swaps; mismatched
            # vector_dim corrupts cosine search silently. Fail loudly.
            info = await client.get_collection(self._collection)
            cfg = info.config.params.vectors
            existing_dim = getattr(cfg, "size", None) or getattr(
                getattr(cfg, "params", None), "size", None
            )
            if existing_dim and int(existing_dim) != int(self._dim):
                raise RuntimeError(
                    f"Qdrant collection '{self._collection}' has vector "
                    f"dim {existing_dim} but settings.vector_dim={self._dim}. "
                    "Recreate the collection or revert OLLAMA_EMBED_MODEL."
                )
        self._ensured = True

    @staticmethod
    def _point_id(*, tenant_id: str, source: str, chunk_index: int) -> str:
        """UUIDv5 so re-upload of the same textbook upserts (overwrites)."""
        return str(uuid.uuid5(_NAMESPACE, f"{tenant_id}/{source}:{chunk_index}"))

    async def upsert_textbook_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str,
        grade: int,
        title: str,
        source: str,
        uploaded_by: uuid.UUID | None,
        chunks: list[dict],
        file_sha256: str | None = None,
    ) -> int:
        """Upsert chunk dicts as Qdrant points.

        Each entry in `chunks` must carry `content: str` and
        `embedding: list[float]`. Optional traceability fields baked into
        the payload so paper-gen retrieval can hard-filter:

          chapter        — chapter order number (int) from the pack
          topic          — topic name (str) best-effort tagged at build
          chunk_type     — "content" (default) or "exercise"

        `chapter_id` / `topic_id` are left null here; the curriculum
        apply-step backfills them once Postgres chapter/topic rows exist
        (see `tag_chunks_with_curriculum`). `source` makes point ids
        stable across re-uploads.
        """
        if not chunks:
            return 0
        await self.ensure_collection()
        client = await self._ready()
        now = datetime.now(timezone.utc).isoformat()
        tid_s = str(tenant_id)

        batch: list[PointStruct] = []
        total = 0
        for chunk_index, ck in enumerate(chunks):
            vec = ck.get("embedding") or []
            text_piece = ck.get("content") or ""
            if len(vec) != self._dim:
                raise ValueError(
                    f"vector dim {len(vec)} != expected {self._dim}")
            payload: dict = {
                "tenant_id": tid_s,
                "subject": subject,
                "grade": int(grade),
                "title": title,
                "source": source,
                "chunk_index": chunk_index,
                "content": text_piece,
                "uploaded_by": str(uploaded_by) if uploaded_by else None,
                "file_sha256": file_sha256,
                "embedded_at": now,
                "chunk_type": str(ck.get("chunk_type") or "content"),
                # Curriculum tags: chapter_order comes from the pack;
                # chapter_id/topic_id are filled in post-apply.
                "chapter_order": (
                    int(ck["chapter"]) if ck.get("chapter") is not None else None
                ),
                "topic_name": ck.get("topic") or None,
                "chapter_id": None,
                "topic_id": None,
            }
            batch.append(PointStruct(
                id=self._point_id(tenant_id=tid_s, source=source,
                                   chunk_index=chunk_index),
                vector=[float(x) for x in vec],
                payload=payload,
            ))
            if len(batch) >= self._batch:
                await client.upsert(self._collection, points=batch, wait=True)
                total += len(batch); batch = []
        if batch:
            await client.upsert(self._collection, points=batch, wait=True)
            total += len(batch)
        logger.info("qdrant.upsert", tenant=tid_s, source=source,
                    points=total)
        return total

    async def tag_chunks_with_curriculum(
        self,
        *,
        tenant_id: uuid.UUID,
        source: str,
        chapter_id_by_order: dict[int, uuid.UUID],
        topic_id_by_order_name: dict[tuple[int, str], uuid.UUID],
    ) -> int:
        """Backfill chapter_id / topic_id on existing chunks.

        Called from `apply_proposal` once Postgres Chapter and Topic rows
        have been created.  Matches each chunk's stored `chapter_order`
        (and `topic_name`) against the supplied maps and merges the
        resolved UUIDs into the payload.  Idempotent — re-running just
        overwrites the same fields.

        Returns the total number of (chapter,topic) set_payload calls
        attempted (not the point count touched).
        """
        if not chapter_id_by_order and not topic_id_by_order_name:
            return 0
        await self.ensure_collection()
        client = await self._ready()
        tid_s = str(tenant_id)
        calls = 0

        for ch_order, chapter_id in chapter_id_by_order.items():
            flt = Filter(must=[
                FieldCondition(key="tenant_id",
                                match=MatchValue(value=tid_s)),
                FieldCondition(key="source", match=MatchValue(value=source)),
                FieldCondition(key="chapter_order",
                                match=MatchValue(value=int(ch_order))),
            ])
            try:
                await client.set_payload(
                    collection_name=self._collection,
                    payload={"chapter_id": str(chapter_id)},
                    points_selector=flt, wait=True,
                )
                calls += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("qdrant.tag_chapter_failed",
                               source=source, chapter_order=ch_order,
                               error=str(exc))

        for (ch_order, topic_name), topic_id in topic_id_by_order_name.items():
            if not topic_name:
                continue
            flt = Filter(must=[
                FieldCondition(key="tenant_id",
                                match=MatchValue(value=tid_s)),
                FieldCondition(key="source", match=MatchValue(value=source)),
                FieldCondition(key="chapter_order",
                                match=MatchValue(value=int(ch_order))),
                FieldCondition(key="topic_name",
                                match=MatchValue(value=topic_name)),
            ])
            try:
                await client.set_payload(
                    collection_name=self._collection,
                    payload={"topic_id": str(topic_id)},
                    points_selector=flt, wait=True,
                )
                calls += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("qdrant.tag_topic_failed",
                               source=source, chapter_order=ch_order,
                               topic=topic_name, error=str(exc))
        logger.info("qdrant.tag_chunks", tenant=tid_s, source=source,
                    chapters=len(chapter_id_by_order),
                    topics=len(topic_id_by_order_name))
        return calls

    async def delete_textbook(self, *, tenant_id: uuid.UUID, source: str) -> int:
        """Drop every point for a (tenant, source) pair so a re-upload starts
        clean (otherwise stale chunks beyond the new chunk count would
        linger — UUIDv5 collisions only overwrite same-index points)."""
        await self.ensure_collection()
        client = await self._ready()
        flt = Filter(must=[
            FieldCondition(key="tenant_id",
                            match=MatchValue(value=str(tenant_id))),
            FieldCondition(key="source", match=MatchValue(value=source)),
        ])
        res = await client.delete(self._collection, points_selector=flt, wait=True)
        return getattr(res, "operation_id", 0) or 0

    async def search_textbook(
        self,
        *,
        query_vector: list[float],
        tenant_id: uuid.UUID,
        subject: str | None = None,
        grade: int | None = None,
        limit: int = 5,
        chapter_ids: list[uuid.UUID] | list[str] | None = None,
        topic_ids: list[uuid.UUID] | list[str] | None = None,
        chunk_types: list[str] | None = None,
    ) -> list[TextbookHit]:
        """Vector-search the textbook collection with optional hard filters.

        `chapter_ids` and `topic_ids` are the curriculum tags written
        during pack apply; passing them constrains retrieval to the
        teacher's selected chapter/topic so the LLM cannot grab content
        from a sibling section. `chunk_types` defaults to ["content"] —
        exercise chunks live in the same collection but are excluded
        from question generation unless the caller asks for them.
        """
        # Defensive guard: tenant_id is the only RBAC layer on this index.
        # If a refactor ever default-args it to None, this catches it
        # before anyone leaks another tenant's textbook chunks.
        if not tenant_id:
            raise ValueError("search_textbook requires a tenant_id")
        await self.ensure_collection()
        client = await self._ready()
        must: list[FieldCondition] = [
            FieldCondition(key="tenant_id",
                            match=MatchValue(value=str(tenant_id))),
        ]
        if subject:
            must.append(FieldCondition(
                key="subject", match=MatchValue(value=subject)))
        if grade is not None:
            must.append(FieldCondition(
                key="grade", match=MatchValue(value=int(grade))))
        if chapter_ids:
            must.append(FieldCondition(
                key="chapter_id",
                match=MatchAny(any=[str(c) for c in chapter_ids]),
            ))
        if topic_ids:
            must.append(FieldCondition(
                key="topic_id",
                match=MatchAny(any=[str(t) for t in topic_ids]),
            ))
        # Default to content chunks only: exercise chunks bias the LLM
        # toward copying questions verbatim from the textbook, which is
        # not what paper-gen wants.
        types = chunk_types if chunk_types is not None else ["content"]
        if types:
            must.append(FieldCondition(
                key="chunk_type", match=MatchAny(any=list(types)),
            ))
        result = await client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=Filter(must=must),
            limit=limit,
            with_payload=True,
        )
        out: list[TextbookHit] = []
        for p in result.points:
            payload = p.payload or {}
            ch_order = payload.get("chapter_order")
            out.append(TextbookHit(
                score=float(p.score),
                content=str(payload.get("content", "")),
                tenant_id=payload.get("tenant_id"),
                subject=payload.get("subject"),
                grade=payload.get("grade"),
                chapter=payload.get("chapter"),
                title=payload.get("title"),
                chunk_index=int(payload.get("chunk_index", 0)),
                point_id=str(p.id) if p.id is not None else None,
                chapter_id=payload.get("chapter_id"),
                topic_id=payload.get("topic_id"),
                chapter_order=int(ch_order) if ch_order is not None else None,
                topic_name=payload.get("topic_name"),
                chunk_type=str(payload.get("chunk_type") or "content"),
                source=payload.get("source"),
                page=payload.get("page"),
            ))
        return out

    async def get_chunks_by_ids(
        self, *, tenant_id: uuid.UUID, point_ids: list[str],
    ) -> list[TextbookHit]:
        """Fetch chunks by Qdrant point id, tenant-scoped.

        Used by the per-question sources endpoint so the UI can show the
        verbatim textbook excerpts that grounded a generated question.
        """
        if not point_ids:
            return []
        await self.ensure_collection()
        client = await self._ready()
        res = await client.retrieve(
            collection_name=self._collection,
            ids=list(point_ids), with_payload=True,
        )
        tid_s = str(tenant_id)
        out: list[TextbookHit] = []
        for p in res:
            payload = p.payload or {}
            # Defensive tenant check — retrieve doesn't filter, so we do.
            if payload.get("tenant_id") != tid_s:
                continue
            ch_order = payload.get("chapter_order")
            out.append(TextbookHit(
                score=1.0,
                content=str(payload.get("content", "")),
                tenant_id=payload.get("tenant_id"),
                subject=payload.get("subject"),
                grade=payload.get("grade"),
                chapter=payload.get("chapter"),
                title=payload.get("title"),
                chunk_index=int(payload.get("chunk_index", 0)),
                point_id=str(p.id) if p.id is not None else None,
                chapter_id=payload.get("chapter_id"),
                topic_id=payload.get("topic_id"),
                chapter_order=int(ch_order) if ch_order is not None else None,
                topic_name=payload.get("topic_name"),
                chunk_type=str(payload.get("chunk_type") or "content"),
                source=payload.get("source"),
                page=payload.get("page"),
            ))
        return out

    async def coverage_for(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str | None = None,
        grade: int | None = None,
    ) -> dict:
        """Group chunks for the admin coverage view.

        Returns:
            {
              "by_chapter": {chapter_id: {"content": n, "exercise": m,
                                          "untagged": k}},
              "by_topic":   {topic_id:   {"content": n, "exercise": m}},
              "untagged_chunks": int,   # chunks with no chapter_id yet
              "total": int,
            }
        Uses a single scroll over (tenant, subject, grade) so the admin
        UI can show one card per chapter with its chunk counts.
        """
        await self.ensure_collection()
        client = await self._ready()
        must: list[FieldCondition] = [
            FieldCondition(key="tenant_id",
                            match=MatchValue(value=str(tenant_id))),
        ]
        if subject:
            must.append(FieldCondition(
                key="subject", match=MatchValue(value=subject)))
        if grade is not None:
            must.append(FieldCondition(
                key="grade", match=MatchValue(value=int(grade))))
        flt = Filter(must=must)

        by_chapter: dict[str, dict[str, int]] = {}
        by_topic: dict[str, dict[str, int]] = {}
        untagged = 0
        total = 0
        next_off = None
        while True:
            points, next_off = await client.scroll(
                self._collection, scroll_filter=flt,
                limit=1024, with_payload=True, offset=next_off,
            )
            for p in points:
                pl = p.payload or {}
                total += 1
                ctype = str(pl.get("chunk_type") or "content")
                ch_id = pl.get("chapter_id")
                tp_id = pl.get("topic_id")
                if ch_id:
                    bucket = by_chapter.setdefault(
                        str(ch_id), {"content": 0, "exercise": 0, "untagged": 0}
                    )
                    bucket[ctype] = bucket.get(ctype, 0) + 1
                else:
                    untagged += 1
                if tp_id:
                    tb = by_topic.setdefault(
                        str(tp_id), {"content": 0, "exercise": 0}
                    )
                    tb[ctype] = tb.get(ctype, 0) + 1
            if not next_off:
                break
        return {
            "by_chapter": by_chapter,
            "by_topic": by_topic,
            "untagged_chunks": untagged,
            "total": total,
        }

    async def count_for_tenant(self, *, tenant_id: uuid.UUID) -> int:
        await self.ensure_collection()
        client = await self._ready()
        flt = Filter(must=[FieldCondition(
            key="tenant_id", match=MatchValue(value=str(tenant_id)))])
        res = await client.count(self._collection, count_filter=flt, exact=True)
        return int(res.count)

    async def existing_sha_for_source(
        self, *, tenant_id: uuid.UUID, source: str
    ) -> tuple[str | None, int]:
        """Return (file_sha256, point_count) for a tenant+source pair, or
        (None, 0) when nothing is stored. Used to skip re-embedding when
        a re-upload's content is identical to the prior upload."""
        await self.ensure_collection()
        client = await self._ready()
        flt = Filter(must=[
            FieldCondition(key="tenant_id",
                            match=MatchValue(value=str(tenant_id))),
            FieldCondition(key="source", match=MatchValue(value=source)),
        ])
        points, _ = await client.scroll(
            self._collection, scroll_filter=flt, limit=1, with_payload=True,
        )
        if not points:
            return None, 0
        sha = (points[0].payload or {}).get("file_sha256")
        count = await client.count(self._collection, count_filter=flt, exact=True)
        return sha, int(count.count)

    async def list_textbooks_for_tenant(
        self, *, tenant_id: uuid.UUID
    ) -> list[dict]:
        """Group points by (subject, grade, source) so the admin UI can
        show one card per uploaded textbook with chunk counts."""
        await self.ensure_collection()
        client = await self._ready()
        flt = Filter(must=[FieldCondition(
            key="tenant_id", match=MatchValue(value=str(tenant_id)))])

        seen: dict[tuple[str | None, int | None, str | None], dict] = {}
        next_off = None
        while True:
            points, next_off = await client.scroll(
                self._collection, scroll_filter=flt,
                limit=1024, with_payload=True, offset=next_off,
            )
            for p in points:
                pl = p.payload or {}
                key = (pl.get("subject"), pl.get("grade"), pl.get("source"))
                entry = seen.setdefault(key, {
                    "subject": pl.get("subject"),
                    "grade": pl.get("grade"),
                    "source": pl.get("source"),
                    "title": pl.get("title"),
                    "chunks": 0,
                    "embedded_at": pl.get("embedded_at"),
                })
                entry["chunks"] += 1
                # Keep the newest embedded_at as the display timestamp
                if pl.get("embedded_at", "") > (entry["embedded_at"] or ""):
                    entry["embedded_at"] = pl.get("embedded_at")
            if not next_off:
                break
        return sorted(seen.values(),
                      key=lambda r: (r["subject"] or "", r["grade"] or 0,
                                     r["source"] or ""))


_instance: QdrantStore | None = None


def get_qdrant_store() -> QdrantStore:
    global _instance
    if _instance is None:
        _instance = QdrantStore()
    return _instance
