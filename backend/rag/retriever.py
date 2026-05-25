from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.db.models import UserRole
from backend.rag.embeddings import get_embedder
from backend.rag.identity import IdentityClaims
from backend.rag.intent import Intent

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    title: str
    kind: str
    content: str
    score: float                # fused RRF score; higher = better. Not bounded to [0,1].
    cosine: float | None        # similarity for diagnostics (None if BM25-only hit)
    bm25: float | None          # ts_rank_cd score (None if vector-only hit)
    subject: str | None
    topic: str | None
    student_id: str | None
    classroom_id: str | None
    created_at: datetime | None


class Retriever:
    """Hybrid (BM25 + vector) retrieval with metadata pre-filter.

    Layout:
      1. Single SQL query selects the union of top-N by cosine and
         top-N by ts_rank_cd, both pre-filtered by tenant +
         role_visibility + scope clauses.
      2. We compute Reciprocal Rank Fusion in Python — for each chunk
         that appears in either ranking, score += 1 / (RRF_K + rank).
         Chunks that appear in both rankings naturally score higher.
      3. Optional recency boost on announcements (the only kind where
         "fresh" beats "best match" — policies and curriculum are stable).
      4. Apply MIN_RANK_SCORE floor + take top K.

    The metadata WHERE clause is identical to the pre-Phase-C version —
    RBAC enforcement happens once, before either ranking function runs.
    No cross-tenant chunk can leak into either arm.
    """

    DEFAULT_K = 5
    OVERFETCH = 15              # how many to pull from each arm before fusion
    RRF_K = 60                  # standard constant; smoothes the rank curve
    MIN_RRF_SCORE = 0.005       # below this we drop the chunk (≈ rank > 200)
    ANNOUNCEMENT_HALF_LIFE_DAYS = 30  # announcements at this age get 0.5x

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def retrieve(
        self,
        query: str,
        identity: IdentityClaims,
        intent: Intent | None = None,
        *,
        k: int = DEFAULT_K,
        kinds: list[str] | None = None,
        subject: str | None = None,
    ) -> list[RetrievedChunk]:
        q = (query or "").strip()
        if not q:
            return []

        embedder = get_embedder()
        try:
            qvec = await embedder.embed(q)
        except Exception as exc:  # noqa: BLE001 — RAG is optional, degrade
            logger.warning("rag.embed_failed", error=str(exc))
            qvec = None  # vector arm unavailable, BM25 still runs

        if kinds is None and intent is not None:
            kinds = _kinds_for_intent(intent)

        sql, params = self._build_hybrid_query(
            qvec=qvec, query_text=q, identity=identity, kinds=kinds, subject=subject,
        )

        result = await self._session.execute(sql, params)
        rows = result.mappings().all()

        # Reciprocal Rank Fusion. Each chunk lands in either or both
        # ranked lists (vector_rank, bm25_rank). RRF score = sum(1/(K+rank))
        # across the lists where the chunk appears.
        scored: dict[str, dict] = {}
        for r in rows:
            cid = str(r["chunk_id"])
            entry = scored.setdefault(cid, {"row": r, "rrf": 0.0, "cosine": None, "bm25": None})
            vrank = r["vector_rank"]
            brank = r["bm25_rank"]
            if vrank is not None:
                entry["rrf"] += 1.0 / (self.RRF_K + vrank)
                entry["cosine"] = float(1.0 - r["vector_distance"]) if r["vector_distance"] is not None else None
            if brank is not None:
                entry["rrf"] += 1.0 / (self.RRF_K + brank)
                entry["bm25"] = float(r["bm25_score"]) if r["bm25_score"] is not None else None

        chunks: list[RetrievedChunk] = []
        for cid, info in scored.items():
            r = info["row"]
            score = info["rrf"]
            if r["kind"] == "announcement":
                score *= self._recency_multiplier(r["created_at"])
            if score < self.MIN_RRF_SCORE:
                continue
            chunks.append(RetrievedChunk(
                chunk_id=cid,
                document_id=str(r["document_id"]),
                title=str(r["title"]),
                kind=str(r["kind"]),
                content=str(r["content"]),
                score=round(score, 4),
                cosine=round(info["cosine"], 3) if info["cosine"] is not None else None,
                bm25=round(info["bm25"], 3) if info["bm25"] is not None else None,
                subject=r["subject"],
                topic=r["topic"],
                student_id=str(r["chunk_student_id"]) if r["chunk_student_id"] else None,
                classroom_id=str(r["chunk_classroom_id"]) if r["chunk_classroom_id"] else None,
                created_at=r["created_at"],
            ))

        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:k]

    # ── query construction ──────────────────────────────────

    def _build_hybrid_query(
        self,
        *,
        qvec: list[float] | None,
        query_text: str,
        identity: IdentityClaims,
        kinds: list[str] | None,
        subject: str | None = None,
    ):
        scope_clause, scope_params = self._scope_clause(identity)
        kind_clause, kind_params = self._kind_clause(kinds)
        # Optional subject narrowing — used by question-paper generation to
        # pull only the chosen subject's textbook chunks.
        subject_clause = "AND c.subject = :subject" if subject else ""
        subject_params = {"subject": subject} if subject else {}

        # Filter block is identical for both retrieval arms — same RBAC
        # enforcement, same kind narrowing, same scope. The two arms only
        # differ in their ranking function.
        filter_block = f"""
              c.tenant_id = :tenant_id
              AND c.role_visibility && :role_vis
              {scope_clause}
              {kind_clause}
              {subject_clause}
        """

        if qvec is not None:
            vector_cte = f"""
                vector_arm AS (
                    SELECT c.id,
                           ROW_NUMBER() OVER (ORDER BY c.embedding <=> :qvec) AS rnk,
                           (c.embedding <=> :qvec) AS distance
                    FROM rag_document_chunks c
                    JOIN rag_documents d ON d.id = c.document_id
                    WHERE c.embedding IS NOT NULL
                      AND {filter_block}
                    ORDER BY c.embedding <=> :qvec
                    LIMIT :overfetch
                )
            """
            vector_join = "LEFT JOIN vector_arm va ON va.id = c.id"
            vector_cols = "va.rnk AS vector_rank, va.distance AS vector_distance"
            vector_or = "OR va.rnk IS NOT NULL"
        else:
            vector_cte = ""
            vector_join = ""
            vector_cols = "NULL::int AS vector_rank, NULL::float AS vector_distance"
            vector_or = ""

        bm25_cte = f"""
            bm25_arm AS (
                SELECT c.id,
                       ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.content_tsv, q) DESC) AS rnk,
                       ts_rank_cd(c.content_tsv, q) AS score
                FROM rag_document_chunks c
                JOIN rag_documents d ON d.id = c.document_id
                CROSS JOIN LATERAL plainto_tsquery('english', :qtext) AS q
                WHERE c.content_tsv @@ q
                  AND {filter_block}
                ORDER BY ts_rank_cd(c.content_tsv, q) DESC
                LIMIT :overfetch
            )
        """

        ctes = ", ".join(c.strip() for c in [vector_cte, bm25_cte] if c.strip())

        sql_text = f"""
            WITH {ctes}
            SELECT
                c.id           AS chunk_id,
                c.document_id  AS document_id,
                d.title        AS title,
                d.kind         AS kind,
                c.content      AS content,
                c.subject      AS subject,
                c.topic        AS topic,
                c.student_id   AS chunk_student_id,
                c.classroom_id AS chunk_classroom_id,
                d.created_at   AS created_at,
                ba.rnk         AS bm25_rank,
                ba.score       AS bm25_score,
                {vector_cols}
            FROM rag_document_chunks c
            JOIN rag_documents d ON d.id = c.document_id
            LEFT JOIN bm25_arm ba ON ba.id = c.id
            {vector_join}
            WHERE (ba.rnk IS NOT NULL {vector_or})
        """

        params: dict = {
            "tenant_id": identity.tenant_id,
            "role_vis": list(identity.role_visibility),
            "qtext": query_text,
            "overfetch": self.OVERFETCH,
            **scope_params,
            **kind_params,
            **subject_params,
        }
        if qvec is not None:
            params["qvec"] = qvec

        stmt = text(sql_text)
        if qvec is not None:
            stmt = stmt.bindparams(bindparam("qvec", type_=Vector(len(qvec))))
        return stmt, params

    def _scope_clause(self, identity: IdentityClaims) -> tuple[str, dict]:
        if identity.role is UserRole.school_admin:
            return "", {}

        params: dict = {}
        clauses: list[str] = []

        general = "(c.student_id IS NULL AND c.classroom_id IS NULL)"
        clauses.append(general)

        if identity.student_ids:
            params["student_ids"] = list(identity.student_ids)
            clauses.append("c.student_id = ANY(:student_ids)")

        if identity.classroom_ids:
            params["classroom_ids"] = list(identity.classroom_ids)
            clauses.append("c.classroom_id = ANY(:classroom_ids)")

        scope = " OR ".join(clauses)
        return f"AND ({scope})", params

    def _kind_clause(self, kinds: list[str] | None) -> tuple[str, dict]:
        if not kinds:
            return "", {}
        return "AND EXISTS (SELECT 1 FROM rag_documents d2 WHERE d2.id = c.document_id AND d2.kind = ANY(:kinds))", {"kinds": kinds}

    def _recency_multiplier(self, created_at) -> float:
        if created_at is None:
            return 1.0
        try:
            if created_at.tzinfo is None:
                ts = created_at.replace(tzinfo=timezone.utc)
            else:
                ts = created_at
            age_days = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
        except Exception:  # noqa: BLE001
            return 1.0
        # exponential decay: 1.0 at age 0, 0.5 at half-life, asymptote toward 0.
        return math.pow(0.5, age_days / self.ANNOUNCEMENT_HALF_LIFE_DAYS)


def _kinds_for_intent(intent: Intent) -> list[str] | None:
    if intent is Intent.curriculum_qa:
        return ["curriculum", "note", "upload"]
    if intent is Intent.policy_qa:
        return ["policy", "upload"]
    if intent is Intent.announcement_recall:
        return ["announcement"]
    return None
